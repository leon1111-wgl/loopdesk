"""Turn a document into indexed chunks (chunk -> injection scan -> embed -> store)."""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..guardrails.injection import detect_injection
from ..llm.base import Embedder
from ..models import Chunk, Document, KnowledgeConflict, Tenant
from .chunking import chunk_markdown, chunk_pages
from .conflicts import detect_conflicts
from .embeddings import pack
from .retrieval import index_manager


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
    vectors = embedder.embed([f"{d.heading}\n{d.text}" for d in drafts]) if drafts else []
    flagged = 0
    for ord_, (draft, vector) in enumerate(zip(drafts, vectors)):
        is_flagged = detect_injection(draft.text).suspicious
        flagged += int(is_flagged)
        db.add(
            Chunk(
                tenant_id=tenant_id,
                document_id=document.id,
                ord=ord_,
                heading=draft.heading,
                text=draft.text,
                page=draft.page,
                embedding=pack(vector),
                flagged_injection=is_flagged,
            )
        )
    document.chunk_count = len(drafts)
    document.flagged_chunks = flagged
    _bump_kb_version(db, tenant_id)
    db.commit()
    index_manager.invalidate(tenant_id)
    detect_conflicts(db, document)
    return document


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


def delete_document(db: Session, document: Document) -> None:
    # The tenant row first, like every path that also touches conflicts (resolving one takes the
    # tenant row for its audit number): one lock order, no deadlock on PostgreSQL.
    _bump_kb_version(db, document.tenant_id)
    db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    db.execute(delete(KnowledgeConflict).where(
        (KnowledgeConflict.document_id == document.id) | (KnowledgeConflict.other_document_id == document.id)))
    tenant_id = document.tenant_id
    db.delete(document)
    db.commit()
    index_manager.invalidate(tenant_id)
