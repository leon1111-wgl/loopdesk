from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..audit import changes, record, text_diff
from ..db import get_db
from ..guardrails.injection import REASON_TEXT, links
from ..models import Chunk, Document, KnowledgeConflict
from ..rag.chunking import UnsupportedFile, parse_upload
from ..rag.conflicts import detect_conflicts, open_conflict_counts
from ..rag.ingest import (StaleDocument, delete_document, edit_document, ingest_document, release_chunk,
                          tenant_channels)
from ..rag.retrieval import cosine_gate, hybrid_search, index_manager, passes_gate
from ..security import Principal, require_admin, require_staff
from ..services import get_services
from .common import get_owned, iso

router = APIRouter(prefix="/api/kb", tags=["knowledge"])
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class TextDocIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=200_000)
    visibility: str = Field(default="public", pattern="^(public|internal)$")


class DocumentEditIn(TextDocIn):
    version: int  # the version the edit was made on (optimistic locking)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    scope: str = Field(default="customer", pattern="^(customer|staff)$")
    top_k: int = Field(default=5, ge=1, le=10)


def document_out(d: Document, conflicts: int = 0) -> dict:
    return {"id": d.id, "title": d.title, "source": d.source, "visibility": d.visibility, "version": d.version,
            "chunk_count": d.chunk_count, "flagged_chunks": d.flagged_chunks, "conflicts": conflicts,
            "created_at": iso(d.created_at), "updated_at": iso(d.updated_at)}


def _with_conflicts(db: Session, d: Document) -> dict:
    return document_out(d, open_conflict_counts(db, d.tenant_id).get(d.id, 0))


@router.get("/documents")
def list_documents(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(Document).where(Document.tenant_id == principal.tenant.id).order_by(Document.id)
    ).scalars()
    counts = open_conflict_counts(db, principal.tenant.id)
    return [document_out(d, counts.get(d.id, 0)) for d in rows]


@router.get("/documents/{document_id}")
def get_document(document_id: int, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    document = get_owned(db, Document, document_id, principal.tenant.id)
    chunks = db.execute(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ord)).scalars()
    return {
        **_with_conflicts(db, document),
        "content": document.content,
        "chunks": [{"id": c.id, "ord": c.ord, "heading": c.heading, "text": c.text, "page": c.page,
                    "flagged_injection": c.flagged_injection,
                    "flag_reasons": [{"code": r, "text": REASON_TEXT.get(r, r)} for r in c.flag_reasons or []],
                    "reviewed_at": iso(c.reviewed_at)} for c in chunks],
    }


@router.get("/domains")
def knowledge_domains(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    """The sites the tenant's knowledge links to, for setting its official channels: how many
    chunks mention each, how many of those are held back, and whether it is official already."""
    channels = tenant_channels(db, principal.tenant.id)
    found: dict[str, dict] = {}
    rows = db.execute(select(Chunk.text, Chunk.flagged_injection).where(Chunk.tenant_id == principal.tenant.id)).all()
    for text, held in rows:
        for host in {host for host, _ in links(text)}:
            entry = found.setdefault(host, {"domain": host, "chunks": 0, "held_chunks": 0,
                                            "official": channels.official_host(host)})
            entry["chunks"] += 1
            entry["held_chunks"] += bool(held)
    return sorted(found.values(), key=lambda d: (d["official"], -d["chunks"], d["domain"]))


@router.post("/chunks/{chunk_id}/release")
def release_held_chunk(chunk_id: int, principal: Principal = Depends(require_admin),
                       db: Session = Depends(get_db)) -> dict:
    """An administrator read a chunk the scan held back and judged it harmless (review round 5,
    N-038): it is indexed again, the decision is audited with the reasons that were reviewed.
    The answer check still stops answers that ask customers for credentials or money unless they
    quote a clause of this chunk word for word; links and contact accounts are never exempted this
    way: if they are the tenant's, they belong in its official channels."""
    chunk = get_owned(db, Chunk, chunk_id, principal.tenant.id)
    document = db.get(Document, chunk.document_id)
    reasons = list(chunk.flag_reasons or [])
    if not release_chunk(db, chunk, principal.user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "这个片段没有被拦截，或已经被其他管理员放行")
    record(db, principal, "kb.chunk.release", f"chunk:{chunk_id}", document_id=document.id, title=document.title,
           reasons=reasons)
    db.commit()
    index_manager.invalidate(principal.tenant.id)
    detect_conflicts(db, document)  # its sentences count now
    return get_document(document.id, principal, db)


@router.post("/documents/text")
def create_text_document(body: TextDocIn, principal: Principal = Depends(require_admin),
                         db: Session = Depends(get_db)) -> dict:
    document = ingest_document(
        db, tenant_id=principal.tenant.id, title=body.title, content=body.content,
        embedder=get_services().embedder, visibility=body.visibility, source="manual", created_by=principal.user.id,
    )
    record(db, principal, "kb.document.create", f"document:{document.id}", title=document.title,
           visibility=document.visibility, version=document.version)
    db.commit()
    return _with_conflicts(db, document)


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    visibility: str = Form("public"),
    title: str | None = Form(None),
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    if visibility not in ("public", "internal"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "visibility 只能是 public 或 internal")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件不能超过 5MB")
    try:
        text, pages = parse_upload(file.filename or "upload.txt", data)
    except UnsupportedFile as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有从文件中解析出文本")
    name = title or (file.filename or "未命名文档").rsplit(".", 1)[0]
    document = ingest_document(
        db, tenant_id=principal.tenant.id, title=name, content=text, embedder=get_services().embedder,
        visibility=visibility, source=file.filename or "", created_by=principal.user.id, pages=pages,
    )
    record(db, principal, "kb.document.create", f"document:{document.id}", title=document.title,
           visibility=document.visibility, version=document.version, file=file.filename or "")
    db.commit()
    return _with_conflicts(db, document)


@router.put("/documents/{document_id}")
def update_document(document_id: int, body: DocumentEditIn, principal: Principal = Depends(require_admin),
                    db: Session = Depends(get_db)) -> dict:
    document = get_owned(db, Document, document_id, principal.tenant.id)
    if db.execute(select(Chunk.id).where(Chunk.document_id == document.id, Chunk.page.is_not(None)).limit(1)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PDF 文档的片段带页码，请上传新版本文件，不支持在线编辑")
    before = {"title": document.title, "visibility": document.visibility}
    try:
        document, previous = edit_document(db, document, title=body.title, content=body.content,
                                           visibility=body.visibility, embedder=get_services().embedder,
                                           base_version=body.version)
    except StaleDocument:
        raise HTTPException(status.HTTP_409_CONFLICT, "文档已被其他人修改，请重新打开后再编辑") from None
    record(db, principal, "kb.document.update", f"document:{document.id}", title=document.title,
           from_version=body.version, to_version=document.version,
           changes=changes(before, {"title": document.title, "visibility": document.visibility}),
           diff=text_diff(previous, document.content))
    db.commit()
    return _with_conflicts(db, document)


@router.delete("/documents/{document_id}")
def remove_document(document_id: int, principal: Principal = Depends(require_admin),
                    db: Session = Depends(get_db)) -> dict:
    document = get_owned(db, Document, document_id, principal.tenant.id)
    title, version = document.title, document.version
    delete_document(db, document)
    record(db, principal, "kb.document.delete", f"document:{document_id}", title=title, version=version)
    db.commit()
    return {"deleted": document_id}


@router.post("/search")
def search(body: SearchIn, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    """Retrieval debugger: shows BM25 rank, vector rank, fused RRF score and relevance."""
    services = get_services()
    visibility = {"public", "internal"} if body.scope == "staff" else {"public"}
    hits, debug = hybrid_search(db, principal.tenant.id, body.query, services.embedder, visibility,
                                top_k=body.top_k, include_flagged=True)
    flagged = {
        c.id for c in db.execute(
            select(Chunk).where(Chunk.id.in_([h.chunk_id for h in hits]), Chunk.flagged_injection.is_(True))
        ).scalars()
    }
    return {
        "query_tokens": debug.query_tokens,
        "coverage_tokens": debug.coverage_tokens,
        "min_relevance": services.settings.min_relevance,
        "min_cosine": cosine_gate(services.settings),
        "hits": [{**h.as_dict(), "flagged_injection": h.chunk_id in flagged,
                  "passes_threshold": passes_gate(h.coverage, h.cosine, services.settings.min_relevance,
                                                  cosine_gate(services.settings))} for h in hits],
        "bm25": debug.bm25,
        "vector": debug.vector,
    }


class ResolveIn(BaseModel):
    note: str = Field(default="", max_length=500)


@router.get("/conflicts")
def list_conflicts(status_filter: str = "open", principal: Principal = Depends(require_staff),
                   db: Session = Depends(get_db)) -> list[dict]:
    query = select(KnowledgeConflict).where(KnowledgeConflict.tenant_id == principal.tenant.id)
    if status_filter != "all":
        query = query.where(KnowledgeConflict.status == status_filter)
    titles = {d.id: d.title for d in db.execute(
        select(Document).where(Document.tenant_id == principal.tenant.id)).scalars()}
    return [
        {"id": c.id, "status": c.status, "origin": c.origin, "sentence": c.sentence, "other_sentence": c.other_sentence,
         "document": {"id": c.document_id, "title": titles.get(c.document_id, "（已删除）")},
         "other_document": {"id": c.other_document_id, "title": titles.get(c.other_document_id, "（已删除）")},
         "detail": c.detail, "note": c.note, "created_at": iso(c.created_at), "resolved_at": iso(c.resolved_at)}
        for c in db.execute(query.order_by(KnowledgeConflict.id.desc())).scalars()
    ]


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, body: ResolveIn, principal: Principal = Depends(require_admin),
                     db: Session = Depends(get_db)) -> dict:
    from datetime import datetime, timezone

    conflict = get_owned(db, KnowledgeConflict, conflict_id, principal.tenant.id)
    # The audit entry takes the tenant row before the conflict row is touched: deleting a
    # document locks them in that order too.
    record(db, principal, "kb.conflict.resolve", f"conflict:{conflict.id}", sentence=conflict.sentence,
           other_sentence=conflict.other_sentence, note=body.note)
    resolved = db.execute(
        update(KnowledgeConflict).where(KnowledgeConflict.id == conflict.id, KnowledgeConflict.status == "open")
        .values(status="resolved", note=body.note, resolved_by=principal.user.id,
                resolved_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False)
    ).rowcount
    if resolved != 1:  # resolved by someone else, or its document was deleted meanwhile
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该冲突已处理或已不存在")
    db.commit()
    return {"id": conflict.id, "status": "resolved"}
