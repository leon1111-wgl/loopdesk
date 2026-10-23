"""Turn a document into indexed chunks (chunk -> injection scan -> embed -> store)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..guardrails.injection import NO_CHANNELS, Channels, scan_knowledge
from ..llm.base import Embedder
from ..models import Chunk, Document, IndexQuestion, KBPatch, KnowledgeConflict, Tenant
from .chunking import chunk_markdown, chunk_pages
from .conflicts import detect_conflicts
from .embeddings import pack
from .retrieval import index_manager

logger = logging.getLogger(__name__)


def ingest_document(
    db: Session,
    *,
    tenant_id: int,
    title: str,
    content: str,
    embedder: Embedder,
    visibility: str = "public",
    source: str = "",
    created_by: int | None = None,
    pages: list[str] | None = None,
    document: Document | None = None,
    version: int | None = None,
) -> Document:
    if visibility not in ("public", "internal"):
        raise ValueError("visibility must be public or internal")
    if document is None:
        document = Document(
            tenant_id=tenant_id, title=title, content=content, visibility=visibility,
            source=source, created_by=created_by, version=1,
        )
        db.add(document)
        db.flush()
    else:
        document.title = title
        document.content = content
        document.visibility = visibility
        document.version = version if version is not None else document.version + 1
        db.execute(delete(Chunk).where(Chunk.document_id == document.id))

    drafts = chunk_pages(pages) if pages else chunk_markdown(content)
    channels = tenant_channels(db, tenant_id)
    try:
        vectors = [pack(v) for v in embedder.embed([f"{d.heading}\n{d.text}" for d in drafts])] if drafts else []
    except Exception as exc:
        if not transient(exc):  # a wrong key, model or dimension: fail loudly, as before
            raise
        # The embedding service is down: store the chunks without vectors (BM25 finds them at once)
        # and let embed_pending fill them in, instead of failing the upload or the startup (N-078).
        logger.warning("embedding failed for %r, stored without vectors: %s", title, exc)
        vectors = [b""] * len(drafts)
    flagged = 0
    for ord_, (draft, vector) in enumerate(zip(drafts, vectors)):
        reasons = scan_chunk(draft.heading, draft.text, visibility, channels)
        flagged += bool(reasons)
        db.add(
            Chunk(
                tenant_id=tenant_id,
                document_id=document.id,
                ord=ord_,
                heading=draft.heading,
                text=draft.text,
                page=draft.page,
                embedding=vector,
                flagged_injection=bool(reasons),
                flag_reasons=reasons,
            )
        )
    document.chunk_count = len(drafts)
    document.flagged_chunks = flagged
    _bump_kb_version(db, tenant_id)
    db.commit()
    index_manager.invalidate(tenant_id)
    detect_conflicts(db, document)
    return document


def tenant_channels(db: Session, tenant_id: int) -> Channels:
    tenant = db.get(Tenant, tenant_id)
    return Channels.of(tenant.official_domains, tenant.official_contacts) if tenant is not None else NO_CHANNELS


def scan_chunk(heading: str, text: str, visibility: str, channels: Channels = NO_CHANNELS) -> list[str]:
    """Why a chunk is held back for review (guardrails.injection rule names); empty to index it.
    Heading and text are scanned apart, so no match straddles the two."""
    public = visibility == "public"
    return list(dict.fromkeys(scan_knowledge(heading, public, channels) + scan_knowledge(text, public, channels)))


def release_chunk(db: Session, chunk: Chunk, reviewer_id: int) -> bool:
    """An administrator read a held-back chunk and judged it harmless: index it (review round 5,
    N-038; before, a false alarm could only be cleared by rewording the document). The reasons
    stay on the chunk as what was reviewed. False if it is not held back (already released, or
    re-ingested meanwhile). The caller audits and commits; then the index must be invalidated."""
    _bump_kb_version(db, chunk.tenant_id)  # the tenant row first, like every knowledge write
    released = db.execute(
        update(Chunk).where(Chunk.id == chunk.id, Chunk.flagged_injection.is_(True))
        .values(flagged_injection=False, reviewed_by=reviewer_id, reviewed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session="fetch")
    ).rowcount
    if released != 1:
        db.rollback()
        return False
    recount_flagged(db, [chunk.document_id])
    return True


def recount_flagged(db: Session, document_ids: list[int]) -> None:
    held = (select(func.count(Chunk.id)).where(Chunk.document_id == Document.id, Chunk.flagged_injection.is_(True))
            .scalar_subquery())
    db.execute(update(Document).where(Document.id.in_(document_ids)).values(flagged_chunks=held)
               .execution_options(synchronize_session="fetch"))


def transient(exc: Exception) -> bool:
    """An embedding failure worth waiting out: the service is down, slow or rate-limited. A refused
    request (wrong key, model, dimension) is a configuration error and is raised as before."""
    from .local_models import ModelSetupError

    if isinstance(exc, ModelSetupError):  # a local model that cannot run (package missing): configuration
        return False
    text = str(exc)
    if "LOOPDESK_EMBEDDING_DIM" in text:  # OpenAICompatEmbedder: the model returned another width
        return False
    status = re.search(r"HTTP (\d{3})", text)
    return not status or int(status.group(1)) >= 500 or int(status.group(1)) in (408, 429)


def embed_pending(db: Session, embedder: Embedder, batch: int = 256) -> int:
    """Embed the chunks stored without vectors while the embedding service was down (N-078).
    Returns how many were filled in; stops at the first failure (the service is still down).

    Each vector is written with a conditional UPDATE on the text it was made from: a document
    edited meanwhile replaced its chunks (SQLite may reuse the ids), and those are left pending
    rather than given the old text's vectors. No tenant row is locked while chunk rows are held,
    and the knowledge version is bumped once per tenant at the end, in its own transaction."""
    filled, tenants, after = 0, set(), 0
    while True:
        rows = db.execute(select(Chunk.id, Chunk.tenant_id, Chunk.heading, Chunk.text)
                          .where(func.length(Chunk.embedding) == 0, Chunk.id > after)
                          .order_by(Chunk.id).limit(batch)).all()
        db.rollback()  # no snapshot or lock held while waiting for the embedding service
        if not rows:
            break
        after = rows[-1].id
        try:
            vectors = embedder.embed([f"{r.heading}\n{r.text}" for r in rows])
            if len(vectors) != len(rows):
                raise ValueError(f"embedding returned {len(vectors)} vectors for {len(rows)} texts")
        except Exception as exc:
            logger.warning("embedding %d pending chunks failed, will retry: %s", len(rows), exc)
            break
        for r, vector in zip(rows, vectors):
            result = db.execute(update(Chunk)
                                .where(Chunk.id == r.id, Chunk.heading == r.heading, Chunk.text == r.text,
                                       func.length(Chunk.embedding) == 0)
                                .values(embedding=pack(vector)).execution_options(synchronize_session=False))
            if result.rowcount:
                filled += 1
                tenants.add(r.tenant_id)
        db.commit()
    filled += _embed_pending_questions(db, embedder, batch, tenants)
    for tenant_id in sorted(tenants):
        _bump_kb_version(db, tenant_id)
        db.commit()
        index_manager.invalidate(tenant_id)
    return filled


def _embed_pending_questions(db: Session, embedder: Embedder, batch: int, tenants: set[int]) -> int:
    """The same for published index questions (W5) stored while the service was down."""
    filled, after = 0, 0
    while True:
        rows = db.execute(select(IndexQuestion.id, IndexQuestion.tenant_id, IndexQuestion.question)
                          .where(func.length(IndexQuestion.embedding) == 0, IndexQuestion.id > after)
                          .order_by(IndexQuestion.id).limit(batch)).all()
        db.rollback()
        if not rows:
            return filled
        after = rows[-1].id
        try:
            vectors = embedder.embed([r.question for r in rows])
            if len(vectors) != len(rows):
                raise ValueError(f"embedding returned {len(vectors)} vectors for {len(rows)} texts")
        except Exception as exc:
            logger.warning("embedding %d pending questions failed, will retry: %s", len(rows), exc)
            return filled
        for r, vector in zip(rows, vectors):
            result = db.execute(update(IndexQuestion)
                                .where(IndexQuestion.id == r.id, func.length(IndexQuestion.embedding) == 0)
                                .values(embedding=pack(vector)).execution_options(synchronize_session=False))
            if result.rowcount:
                filled += 1
                tenants.add(r.tenant_id)
        db.commit()


def _bump_kb_version(db: Session, tenant_id: int) -> None:
    """Part of the chunk change's transaction, so every process sees the new version exactly
    when it can see the new chunks."""
    db.execute(update(Tenant).where(Tenant.id == tenant_id).values(kb_version=Tenant.kb_version + 1)
               .execution_options(synchronize_session=False))


class StaleDocument(Exception):
    """The document changed after the editor loaded it."""


def edit_document(db: Session, document: Document, *, title: str, content: str, visibility: str,
                  embedder: Embedder, base_version: int) -> tuple[Document, str]:
    """Replace a document's text as its next version, re-chunked, re-indexed and re-checked for
    conflicts (an edit that removes a conflicting sentence closes that conflict).

    Optimistic: the edit names the version it was made on, and one conditional UPDATE claims
    the next one, so of two administrators editing the same version one saves and the other
    is told to reload instead of silently overwriting. The claim also takes SQLite's write
    lock; on PostgreSQL the tenant row lock serialises the edit with flywheel publishing,
    which rewrites the patch document from what it reads. Returns the document and the text
    it replaced (read after the claim, so it is exactly the base version's)."""
    db.commit()
    # FOR NO KEY UPDATE: serialises with other edits and publishes, while inserts that reference
    # the tenant (a chat's runs, messages, spans) still go through.
    db.execute(select(Tenant.id).where(Tenant.id == document.tenant_id).with_for_update(key_share=True))
    claimed = db.execute(
        update(Document).where(Document.id == document.id, Document.version == base_version)
        .values(version=base_version + 1).execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise StaleDocument
    previous = db.execute(select(Document.content).where(Document.id == document.id)).scalar_one()
    document = ingest_document(db, tenant_id=document.tenant_id, title=title, content=content, embedder=embedder,
                               visibility=visibility, source=document.source, document=document,
                               version=base_version + 1)
    return document, previous


def set_document_meta(db: Session, document: Document, *, title: str, visibility: str,
                      base_version: int) -> Document:
    """Change a document's title or visibility without touching its text, as its next version:
    what a PDF needs (its chunks carry page numbers the editor cannot rebuild, N-165) and what an
    administrator who only flips visibility means (N-115). Claimed like `edit_document`. The
    chunks are scanned again under the new visibility, since public knowledge is held to stricter
    rules (instructions to staff, `agent_directive`); a released chunk stays released unless the
    new scan finds a reason the review did not see. The knowledge version is bumped, so answers
    cached from a document that is now internal are not served to customers again."""
    if visibility not in ("public", "internal"):
        raise ValueError("visibility must be public or internal")
    db.commit()
    db.execute(select(Tenant.id).where(Tenant.id == document.tenant_id).with_for_update(key_share=True))
    claimed = db.execute(
        update(Document).where(Document.id == document.id, Document.version == base_version)
        .values(version=base_version + 1, title=title, visibility=visibility)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise StaleDocument
    channels = tenant_channels(db, document.tenant_id)
    for chunk in db.execute(select(Chunk).where(Chunk.document_id == document.id)).scalars():
        stored = list(chunk.flag_reasons or [])
        held, reasons, forget = review_target(scan_chunk(chunk.heading, chunk.text, visibility, channels), stored,
                                              chunk.reviewed_at is not None)
        chunk.flagged_injection, chunk.flag_reasons = held, reasons
        if forget:
            chunk.reviewed_by, chunk.reviewed_at = None, None
    db.flush()
    recount_flagged(db, [document.id])
    _bump_kb_version(db, document.tenant_id)
    db.commit()
    index_manager.invalidate(document.tenant_id)
    db.refresh(document)
    return document


def review_target(reasons: list[str], stored: list[str], reviewed: bool) -> tuple[bool, list[str], bool]:
    """(held back, reasons to store, forget the review) for a chunk the rules now give ``reasons``:
    a chunk an administrator released stays released while nothing beyond what was reviewed is found,
    also after a public -> internal -> public round trip (the internal scan finds nothing)."""
    if not reasons:  # what was reviewed stays on a released chunk, so a later rescan still honours it
        return False, (stored if reviewed else []), False
    if reviewed and set(reasons) <= set(stored):
        return False, stored, False
    return True, reasons, True


def delete_document(db: Session, document: Document) -> None:
    # The tenant row first, like every path that also touches conflicts (resolving one takes the
    # tenant row for its audit number): one lock order, no deadlock on PostgreSQL.
    _bump_kb_version(db, document.tenant_id)
    db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    db.execute(delete(IndexQuestion).where(IndexQuestion.document_id == document.id))
    # Published patches keep their record (the questions patch names its document in the payload).
    db.execute(update(KBPatch).where(KBPatch.document_id == document.id).values(document_id=None)
               .execution_options(synchronize_session=False))
    db.execute(delete(KnowledgeConflict).where(
        (KnowledgeConflict.document_id == document.id) | (KnowledgeConflict.other_document_id == document.id)))
    tenant_id = document.tenant_id
    db.delete(document)
    db.commit()
    index_manager.invalidate(tenant_id)
