"""Colloquial questions as extra index entries of a chunk (W5, after FastGPT's "indexes").

Customers rarely use the knowledge base's words: "包裹到哪儿了在哪查" shares no term with "发货后可在
“我的订单”中查看运单号和物流轨迹", so BM25 misses it and the coverage gate refuses it. A model writes
3-5 customer wordings per chunk ahead of time (never at question time); they are proposed as a
knowledge patch (``kind = "questions"``) and change nothing until an administrator publishes them,
dropping the ones that are wrong. A published question is bound to the exact chunk text it was
written for (sha256): edit that section and the question stops counting until it is reviewed again,
so a question can never route a customer to a passage that no longer answers it.

    python -m app.rag.questions --tenant leon --propose            # the configured LLM writes proposals
    python -m app.rag.questions --tenant leon --export out.json    # proposals (or --stale) as a file
    python -m app.rag.questions --tenant leon --import out.json    # a file written elsewhere -> proposals

The file is how the offline generator hands over: it runs where the model is (GitHub Actions,
retrieval-eval.yml), and any instance imports the result as proposals for its own reviewers.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..agent.prompts import KB_QUESTIONS_PROMPT
from ..guardrails.injection import scan_knowledge
from ..llm.base import Embedder, LLMMessage, LLMProvider
from ..models import Chunk, Document, IndexQuestion, KBPatch, Tenant
from .embeddings import pack
from .retrieval import index_manager, text_sha

logger = logging.getLogger(__name__)

__all__ = ["text_sha", "generate_questions", "propose_questions", "import_questions", "export_questions",
           "publish_questions", "delete_questions"]

MAX_PER_CHUNK = 5
MIN_CHARS, MAX_CHARS = 4, 60
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.、)）]|[（(]\d+[)）])\s*")


def parse_questions(reply: str) -> list[str]:
    """{"questions": [...]}, a bare JSON list, or one question per line (numbering stripped)."""
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1], text[text.find("["):text.rfind("]") + 1]):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            value = value.get("questions")
        if isinstance(value, list):
            return [str(q).strip() for q in value if str(q).strip()]
    return [_BULLET.sub("", line).strip() for line in text.splitlines() if _BULLET.sub("", line).strip()]


def clean_questions(questions: Iterable[str], heading: str) -> list[str]:
    """At most five distinct questions of a sane length that the knowledge scan does not hold back
    (a question is indexed text: an instruction planted in it must not ride along)."""
    leaf = heading.split(" > ")[-1].strip()
    out: list[str] = []
    seen: set[str] = set()
    for question in questions:
        question = " ".join(question.split())
        key = re.sub(r"[\s，。？?！!、,.]", "", question).lower()
        if not (MIN_CHARS <= len(question) <= MAX_CHARS) or key in seen or key == leaf:
            continue
        if scan_knowledge(question, public=True):
            logger.warning("dropped a generated question the knowledge scan holds back: %r", question[:40])
            continue
        seen.add(key)
        out.append(question)
        if len(out) == MAX_PER_CHUNK:
            break
    return out


def generate_questions(provider: LLMProvider, model: str, heading: str, text: str) -> list[str]:
    reply = provider.chat([LLMMessage(role="user", content=KB_QUESTIONS_PROMPT.format(heading=heading, text=text))],
                          [], model, {"task": "kb_questions", "temperature": 0}).content
    return clean_questions(parse_questions(reply), heading)


def _render(title: str, items: list[dict]) -> str:
    """What the reviewer reads in the patch list: the questions under the section they index."""
    lines = [f"# 口语问法：{title}", "", "> 发布后，这些问法作为对应片段的检索索引；不改变片段原文。"]
    for item in items:
        lines += ["", f"## {item['heading']}"] + [f"- {q}" for q in item["questions"]]
    return "\n".join(lines) + "\n"


def _current_chunks(db: Session, document: Document) -> list[Chunk]:
    # Held-back chunks (injection scan) are not indexed, so they get no questions either.
    return db.execute(select(Chunk).where(Chunk.document_id == document.id, Chunk.flagged_injection.is_(False))
                      .order_by(Chunk.ord)).scalars().all()


def _patch(db: Session, document: Document, items: list[dict]) -> KBPatch | None:
    items = [i for i in items if i["questions"]]
    if not items:
        return None
    patch = KBPatch(tenant_id=document.tenant_id, kind="questions", title=f"口语问法：{document.title}",
                    content=_render(document.title, items),
                    payload={"document_id": document.id, "document_title": document.title, "items": items})
    db.add(patch)
    db.commit()
    return patch


def propose_questions(db: Session, document: Document, provider: LLMProvider, model: str) -> KBPatch | None:
    """Ask the model for each chunk of the document; one proposal for the whole document."""
    items = []
    for chunk in _current_chunks(db, document):
        try:
            questions = generate_questions(provider, model, chunk.heading, chunk.text)
        except Exception as exc:  # one failed chunk does not lose the others
            logger.warning("question generation failed for chunk %s: %s", chunk.id, exc)
            continue
        items.append({"heading": chunk.heading, "text_sha": text_sha(chunk.text), "questions": questions})
    return _patch(db, document, items)


def import_questions(db: Session, tenant: Tenant, data: dict) -> list[KBPatch]:
    """Proposals from a file (export_questions' format). Items are matched to this tenant's documents
    by title and to chunks by heading and text: questions written for a text this instance does not
    have are skipped."""
    patches = []
    for doc in data.get("documents") or []:
        document = db.execute(select(Document).where(Document.tenant_id == tenant.id,
                                                     Document.title == doc.get("title"))).scalars().first()
        if document is None:
            continue
        current = {(c.heading, text_sha(c.text)) for c in _current_chunks(db, document)}
        items = [{"heading": i["heading"], "text_sha": i["text_sha"],
                  "questions": clean_questions(i.get("questions") or [], i["heading"])}
                 for i in doc.get("items") or [] if (i.get("heading"), i.get("text_sha")) in current]
        patch = _patch(db, document, items)
        if patch is not None:
            patches.append(patch)
    return patches


def export_questions(db: Session, tenant: Tenant, stale_only: bool = False) -> dict:
    """Proposals waiting for review, or (``stale_only``) published questions whose section text
    changed since: the list to propose again."""
    documents: dict[int, dict] = {}
    if stale_only:
        rows = db.execute(select(IndexQuestion, Document.title).join(Document, Document.id == IndexQuestion.document_id)
                          .where(IndexQuestion.tenant_id == tenant.id).order_by(IndexQuestion.id)).all()
        current: dict[int, set] = {}
        for row, title in rows:
            if row.document_id not in current:
                current[row.document_id] = {(c.heading, text_sha(c.text))
                                            for c in _current_chunks(db, db.get(Document, row.document_id))}
            if (row.heading, row.text_sha) in current[row.document_id]:
                continue
            doc = documents.setdefault(row.document_id, {"title": title, "items": {}})
            doc["items"].setdefault((row.heading, row.text_sha), []).append(row.question)
        docs = [{"title": d["title"], "items": [{"heading": h, "text_sha": sha, "questions": qs}
                                                for (h, sha), qs in d["items"].items()]} for d in documents.values()]
        return {"tenant": tenant.slug, "documents": docs}
    for patch in db.execute(select(KBPatch).where(KBPatch.tenant_id == tenant.id, KBPatch.kind == "questions",
                                                  KBPatch.status == "proposed").order_by(KBPatch.id)).scalars():
        payload = patch.payload or {}
        doc = documents.setdefault(payload.get("document_id"), {"title": payload.get("document_title"), "items": []})
        doc["items"].extend(payload.get("items") or [])
    return {"tenant": tenant.slug, "documents": list(documents.values())}


def publish_questions(db: Session, patch: KBPatch, embedder: Embedder, skip: Iterable[str] = ()) -> dict:
    """Index a reviewed proposal's questions; the caller has claimed the patch and holds the tenant
    row. Embedded first: a transient embedding failure stores them without vectors (BM25 and the
    coverage gate use them at once, embed_pending fills the vectors in), anything else raises."""
    from .ingest import _bump_kb_version, transient

    payload = dict(patch.payload or {})
    document = db.get(Document, payload.get("document_id"))
    if document is None or document.tenant_id != patch.tenant_id:
        raise LookupError("the document of this proposal was deleted")
    current = {(c.heading, text_sha(c.text)) for c in _current_chunks(db, document)}
    skip = set(skip)
    rows, stale = [], 0
    for item in payload.get("items") or []:
        kept = [q for q in item["questions"] if q not in skip]
        if (item["heading"], item["text_sha"]) not in current:
            stale += len(kept)
            continue
        rows += [(item["heading"], item["text_sha"], q) for q in kept]
    try:
        vectors = [pack(v) for v in embedder.embed([q for _, _, q in rows])] if rows else []
    except Exception as exc:
        if not transient(exc):
            raise
        logger.warning("embedding %d published questions failed, stored without vectors: %s", len(rows), exc)
        vectors = [b""] * len(rows)
    for (heading, sha, question), vector in zip(rows, vectors):
        db.add(IndexQuestion(tenant_id=patch.tenant_id, document_id=document.id, heading=heading, text_sha=sha,
                             question=question, embedding=vector, patch_id=patch.id))
    payload["published"] = {"indexed": len(rows), "skipped": sorted(skip), "stale": stale}
    patch.payload = payload
    patch.document_id = document.id
    _bump_kb_version(db, patch.tenant_id)
    return payload["published"]


def delete_questions(db: Session, document_id: int) -> None:
    db.execute(delete(IndexQuestion).where(IndexQuestion.document_id == document_id))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI around the functions above
    parser = argparse.ArgumentParser(description="LoopDesk colloquial index questions")
    parser.add_argument("--tenant", action="append", help="tenant slug (repeatable; default: every tenant)")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--propose", action="store_true", help="ask the configured LLM for proposals")
    action.add_argument("--export", help="write proposals (or --stale questions) to this JSON file")
    action.add_argument("--import", dest="import_file", help="add proposals from this JSON file")
    parser.add_argument("--stale", action="store_true", help="with --export: published questions to review again")
    parser.add_argument("--public-only", action="store_true", help="with --propose: skip internal documents")
    args = parser.parse_args(argv)

    from ..config import get_settings
    from ..db import init_engine, new_session
    from ..llm import build_provider

    settings = get_settings()
    init_engine(settings.database_url)
    with new_session() as db:
        query = select(Tenant).order_by(Tenant.id)
        if args.tenant:
            query = query.where(Tenant.slug.in_(args.tenant))
        tenants = db.execute(query).scalars().all()
        if args.export:
            out = {"tenants": [export_questions(db, t, stale_only=args.stale) for t in tenants]}
            Path(args.export).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"wrote {sum(len(i['questions']) for t in out['tenants'] for d in t['documents'] for i in d['items'])} "
                  f"questions to {args.export}")
            return 0
        if args.import_file:
            data = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
            by_slug = {t["tenant"]: t for t in data.get("tenants", [data])}
            made = [p for t in tenants if t.slug in by_slug for p in import_questions(db, t, by_slug[t.slug])]
            print(f"{len(made)} proposals waiting for review")
            return 0
        provider = build_provider(settings)
        for tenant in tenants:
            documents = db.execute(select(Document).where(Document.tenant_id == tenant.id).order_by(Document.id)).scalars()
            for document in documents:
                if args.public_only and document.visibility != "public":
                    continue
                patch = propose_questions(db, document, provider, settings.llm_model_fast)
                count = sum(len(i["questions"]) for i in patch.payload["items"]) if patch else 0
                print(f"{tenant.slug} · {document.title}: {count} questions proposed", file=sys.stderr)
    index_manager.clear()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
