"""Turn a document into indexed chunks (chunk -> injection scan -> embed -> store)."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..guardrails.injection import detect_injection
from ..llm.base import Embedder
from ..models import Chunk, Document
from .chunking import chunk_markdown, chunk_pages
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
        document.version += 1
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
    db.commit()
    index_manager.invalidate(tenant_id)
    return document


def delete_document(db: Session, document: Document) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    tenant_id = document.tenant_id
    db.delete(document)
    db.commit()
    index_manager.invalidate(tenant_id)
