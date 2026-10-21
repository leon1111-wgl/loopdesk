"""When the embedding service fails (N-078). Before: ``hybrid_search`` always called the embedder, the
tool layer turned its exception into "工具执行异常", and the customer was told "知识库中没有找到与该
问题直接相关的依据" for a question BM25 alone answers (coverage 1.0); the run counted as a refusal,
not a failure. A demo instance whose embedding service was down at startup exited while seeding."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent.outcome import is_refusal
from app.config import Settings
from app.db import new_session
from app.llm.base import Embedder, LLMError
from app.llm.breaker import CircuitBreaker
from app.llm.mock import REFUSAL, SEARCH_DOWN
from app.llm.openai_compat import OpenAICompatEmbedder
from app.main import create_app
from app.agent.tools import Evidence
from app.agent.verifier import verify_answer
from app.models import AnswerCache, Chunk, Document, Tenant
from app.rag.embeddings import GuardedEmbedder, HashingEmbedder, pack
from app.rag.ingest import embed_pending, ingest_document
from app.rag.retrieval import _matrix, backfill, hybrid_search, index_manager
from app.seed import DEMO_PASSWORD
from app.services import get_services
from tests.conftest import by_type


class DownEmbedder(Embedder):
    def __init__(self, dim: int = 512):
        self.dim = dim
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        raise LLMError("embedding HTTP 500: upstream down")


def _break_embeddings(monkeypatch) -> DownEmbedder:
    down = DownEmbedder()
    monkeypatch.setattr(get_services(), "embedder", down)
    return down


def _search_output(events: list[dict]) -> dict:
    return [e for e in by_type(events, "tool_result") if e["name"] == "search_knowledge"][-1]["output"]


def test_a_policy_question_is_answered_from_bm25_when_embeddings_fail(client, ask, login, monkeypatch):
    down = _break_embeddings(monkeypatch)
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    final = by_type(events, "final")[0]
    assert down.calls >= 1
    assert "1-3个工作日" in final["text"] and final["meta"]["citations"], "the policy itself, cited"
    assert not is_refusal(final["text"])
    assert _search_output(events)["degraded"] and _search_output(events)["results"]
    run_id = client.post("/api/chat", json={"content": "退款多久能到账？"},
                         headers=login("customer2@leon.demo")).json()["run_id"]
    trace = client.get(f"/api/runs/{run_id}", headers=login("admin@leon.demo")).json()
    assert "embedding HTTP 500" in json.dumps(trace, ensure_ascii=False), "the trace says why"


def test_a_partial_search_is_never_cached(ask, login, monkeypatch):
    _break_embeddings(monkeypatch)
    ask(login("customer@leon.demo"), "退款多久能到账？")
    with new_session() as db:
        assert db.execute(select(func.count()).select_from(AnswerCache)).scalar() == 0


def test_nothing_found_during_an_outage_is_not_told_as_nothing_in_the_knowledge_base(ask, login, monkeypatch):
    _break_embeddings(monkeypatch)
    _, events = ask(login("customer@leon.demo"), "支持花呗分期吗")
    final = by_type(events, "final")[0]
    assert SEARCH_DOWN in final["text"] and REFUSAL not in final["text"]
    assert not is_refusal(final["text"]), "a failure, not a refusal"
    assert final["meta"]["verification"]["verdict"] == "pass", "saying the search failed claims nothing"
    assert "不代表知识库里没有" in _search_output(events)["note"]


def test_the_same_question_without_an_outage_still_refuses(ask, login):
    _, events = ask(login("customer@leon.demo"), "支持花呗分期吗")
    assert REFUSAL in by_type(events, "final")[0]["text"]


def test_the_breaker_stops_calling_a_dead_embedding_service():
    settings = Settings()
    settings.llm_breaker_failures = 3
    inner = DownEmbedder()
    guarded = GuardedEmbedder(inner, CircuitBreaker(settings), "embedding:test")
    for _ in range(5):
        with pytest.raises(LLMError):
            guarded.embed(["退款"])
    assert inner.calls == 3, "after three failures the service is not called during the cool-down"


def test_a_remote_embedding_error_degrades_the_search(client):
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="down"))
    remote = OpenAICompatEmbedder("http://embed.test/v1", "key", "m", 512, transport=transport)
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).order_by(Tenant.id)).scalars().first()
        hits, debug = hybrid_search(db, tenant_id, "退款多久能到账", remote, {"public"}, min_relevance=0.5)
    assert hits and debug.vector_error and debug.vector == []


def test_documents_uploaded_during_an_outage_are_searchable_and_embedded_later(client, login, monkeypatch):
    admin = login("admin@leon.demo")
    _break_embeddings(monkeypatch)
    r = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "以旧换新", "content": "# 以旧换新\n\n旧手机以旧换新可抵扣 300 元，需在下单后 7 天内寄回旧机。",
    })
    assert r.status_code in (200, 201), r.text
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).order_by(Tenant.id)).scalars().first()
        pending = db.execute(select(func.count()).select_from(Chunk)
                             .where(func.length(Chunk.embedding) == 0)).scalar()
        assert pending >= 1, "stored without vectors instead of failing the upload"
        hits, debug = hybrid_search(db, tenant_id, "以旧换新抵扣多少", get_services().embedder, {"public"},
                                    min_relevance=0.5)
        assert hits and hits[0].doc_title == "以旧换新" and debug.vector_error

    # The service is back: the next search kicks the backfill, which embeds the stored chunks.
    monkeypatch.setattr(backfill, "_last", float("-inf"))
    monkeypatch.setattr(backfill, "kick", lambda embedder, wait=False, kick=backfill.kick: kick(embedder, wait=True))
    working = HashingEmbedder(512)
    with new_session() as db:
        hybrid_search(db, tenant_id, "以旧换新抵扣多少", working, {"public"})
    with new_session() as db:
        assert db.execute(select(func.count()).select_from(Chunk)
                          .where(func.length(Chunk.embedding) == 0)).scalar() == 0
        assert index_manager.get(db, tenant_id).pending == 0
        hits, debug = hybrid_search(db, tenant_id, "以旧换新抵扣多少", working, {"public"}, min_relevance=0.5)
        assert debug.vector_error is None and any(v["chunk_id"] == hits[0].chunk_id for v in debug.vector)


def test_the_demo_starts_when_the_embedding_service_is_down(settings, monkeypatch):
    monkeypatch.setattr("app.services.build_embedder", lambda s: DownEmbedder(s.embedding_dim))
    index_manager._cache.clear()
    index_manager._tokens.clear()
    app = create_app(settings)  # used to raise LLMError from seed_demo -> ingest_document
    with TestClient(app) as c:
        token = c.post("/api/auth/login", json={"email": "customer@leon.demo", "password": DEMO_PASSWORD})
        assert token.status_code == 200, token.text
        headers = {"Authorization": f"Bearer {token.json()['token']}"}
        r = c.post("/api/chat", headers=headers, json={"content": "退款多久能到账？"})
        assert r.status_code == 200 and "1-3个工作日" in r.json()["answer"], r.text


# ---- review round (Claude Fable, read-only): each of these failed on the first version of the fix


def test_an_outage_shows_on_the_dashboard_and_is_not_counted_resolved(client, ask, login, monkeypatch):
    _break_embeddings(monkeypatch)
    ask(login("customer@leon.demo"), "支持花呗分期吗")
    failures = client.get("/api/metrics/overview", headers=login("admin@leon.demo")).json()["failures"]
    assert failures["runs"] == 1 and failures["kinds"] == {"embedding": 1}


def test_saying_the_search_failed_does_not_shield_a_claim_next_to_it():
    evidence = [Evidence(ref=1, kind="kb", title="售后与退款政策", text="退款在1-3个工作日内到账。")]
    for answer in ("抱歉，知识库检索暂时出了故障，本店所有商品支持终身保修。",
                   "检索暂时故障，退款需要三到五个工作日才能到账。"):
        assert verify_answer(answer, evidence, "退款多久到账", Settings()).verdict != "pass", answer
    assert verify_answer(SEARCH_DOWN, evidence, "退款多久到账", Settings()).verdict == "pass"


def test_pending_rows_do_not_switch_off_vector_search_for_the_embedded_ones():
    full = pack([1.0] + [0.0] * 511)
    matrix = _matrix([b"", b"", full])
    assert matrix is not None and matrix.shape == (3, 512) and matrix[2, 0] == 1.0 and not matrix[0].any()


def test_a_configuration_error_still_fails_the_upload(client):
    class WrongDim(Embedder):
        dim = 512

        def embed(self, texts):
            raise LLMError("embedding model m returned 1024 dimensions, LOOPDESK_EMBEDDING_DIM is 512")

    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).order_by(Tenant.id)).scalars().first()
        with pytest.raises(LLMError):
            ingest_document(db, tenant_id=tenant_id, title="t", content="# t\n\n内容", embedder=WrongDim())


def _pending(db) -> int:
    return db.execute(select(func.count()).select_from(Chunk).where(func.length(Chunk.embedding) == 0)).scalar()


def test_a_document_edited_during_the_backfill_keeps_no_stale_vectors(client, monkeypatch):
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).order_by(Tenant.id)).scalars().first()
        doc = ingest_document(db, tenant_id=tenant_id, title="以旧换新", content="# 以旧换新\n\n可抵扣 300 元。",
                              embedder=DownEmbedder())
        doc_id = doc.id
        assert _pending(db) == 1

    class EditsWhileEmbedding(HashingEmbedder):
        def embed(self, texts):
            with new_session() as other:  # an admin saves a new version meanwhile
                ingest_document(other, tenant_id=tenant_id, title="以旧换新", content="# 以旧换新\n\n可抵扣 500 元。",
                                embedder=DownEmbedder(), document=other.get(Document, doc_id))
            return super().embed(texts)

    with new_session() as db:
        assert embed_pending(db, EditsWhileEmbedding(512)) == 0, "the vector of the old text is not written"
        assert _pending(db) == 1
        assert embed_pending(db, HashingEmbedder(512)) == 1 and _pending(db) == 0


def test_a_short_answer_from_the_service_does_not_loop(client):
    class Short(HashingEmbedder):
        def embed(self, texts):
            return []

    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).order_by(Tenant.id)).scalars().first()
        ingest_document(db, tenant_id=tenant_id, title="t", content="# t\n\n内容", embedder=DownEmbedder())
        assert embed_pending(db, Short(512)) == 0 and _pending(db) == 1
