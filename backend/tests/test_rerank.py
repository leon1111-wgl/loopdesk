"""Cross-encoder reranking and the two-signal relevance gate (W5).

The lexical gate (IDF-weighted term coverage) refuses a question whose words the knowledge base
does not use ("邮费谁出" against "运费由我们承担"), and lets through one that shares a word but asks
about something else. A cross-encoder reads the question and the passage together, so its score
is a second, independent signal: a chunk passes on coverage OR on a calibrated rerank score, and a
calibrated floor can veto a coverage pass the reranker scores as unrelated. Models are faked here;
the real ones run only on GitHub Actions (.github/workflows/retrieval-eval.yml)."""

import json
import math

import httpx
import pytest

from app.config import Settings
from app.db import new_session
from app.llm.base import Embedder, LLMError
from app.llm.breaker import CircuitBreaker
from app.models import Tenant
from app.rag.ingest import ingest_document
from app.rag.rerank import GuardedReranker, HTTPReranker, Reranker, build_reranker
from app.rag.retrieval import hybrid_search, passes_gate

TOPICS = {  # a toy "semantic" space: synonyms share a direction, as a real embedding model would
    "运费": 0, "邮费": 0, "寄回": 0,
    "退款": 1, "到账": 1, "钱": 1,
    "发票": 2, "抬头": 2,
}

DOCS = {
    "配送": "## 退货运费\n\n质量问题退货的运费由我们承担，请保留快递单号。",
    "退款": "## 退款到账\n\n退款审核通过后1-3个工作日原路退回。",
    "发票": "## 发票抬头\n\n发票抬头开具后30天内可以申请修改一次。",
}


class TopicEmbedder(Embedder):
    dim = 4

    def embed(self, texts):
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word, axis in TOPICS.items():
                if word in text:
                    vec[axis] += 1.0
            vec[3] = 0.1  # a common direction, like any real model's
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec])
        return out


class TableReranker(Reranker):
    """Scores (question, passage) from a table keyed by a word of each; 0.02 otherwise."""

    name = "table"

    def __init__(self, table: dict[tuple[str, str], float]):
        self.table = table
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query, passages):
        self.calls.append((query, list(passages)))
        return [next((s for (q, p), s in self.table.items() if q in query and p in passage), 0.02)
                for passage in passages]


class BrokenReranker(Reranker):
    name = "broken"

    def score(self, query, passages):
        raise LLMError("rerank HTTP 503: upstream down")


@pytest.fixture()
def tenant(client):
    embedder = TopicEmbedder()
    with new_session() as db:
        t = Tenant(slug="rr", name="RR", industry="ecommerce")
        db.add(t)
        db.commit()
        for title, content in DOCS.items():
            ingest_document(db, tenant_id=t.id, title=title, content=content, embedder=embedder)
        return t.id


def _search(tenant_id, question, **gate):
    gate.setdefault("min_relevance", 0.3)
    with new_session() as db:
        return hybrid_search(db, tenant_id, question, TopicEmbedder(), {"public"}, **gate)


def test_the_reranker_reorders_the_fused_candidates(tenant):
    question = "退货运费和退款到账"  # both chunks cover part of it; the reranker prefers the refund one
    plain, _ = _search(tenant, question, min_relevance=0.0)
    reranker = TableReranker({("退货运费和退款到账", "退款到账"): 0.9, ("退货运费和退款到账", "退货运费"): 0.4})
    reranked, debug = _search(tenant, question, min_relevance=0.0, reranker=reranker)
    assert plain[0].heading == "退货运费", "RRF alone puts the shipping chunk first"
    assert [h.heading for h in reranked[:2]] == ["退款到账", "退货运费"]
    assert reranked[0].rerank == pytest.approx(0.9) and reranked[0].bm25_rank is not None
    assert debug.reranked == len(reranker.calls[0][1])


def test_a_rerank_score_opens_the_gate_only_once_calibrated(tenant):
    # "邮费" is not a word of the knowledge base: coverage 0, the lexical gate refuses.
    reranker = TableReranker({("邮费", "退货运费"): 0.93})
    assert _search(tenant, "邮费谁出")[0] == []
    hits, _ = _search(tenant, "邮费谁出", reranker=reranker)
    assert hits == [], "an uncalibrated reranker reorders, it never lets a chunk through on its own"
    hits, _ = _search(tenant, "邮费谁出", reranker=reranker, min_rerank=0.5)
    assert [h.heading for h in hits] == ["退货运费"]
    assert hits[0].coverage == 0.0 and hits[0].relevance == pytest.approx(0.93)
    hits, _ = _search(tenant, "邮费谁出", reranker=reranker, min_rerank=0.95)
    assert hits == []


def test_a_calibrated_floor_vetoes_a_coverage_pass_the_reranker_calls_unrelated(tenant):
    question = "发票抬头"  # full coverage of the invoice chunk
    reranker = TableReranker({("发票抬头", "发票抬头"): 0.03})
    hits, _ = _search(tenant, question, reranker=reranker, min_rerank=0.5)
    assert [h.heading for h in hits] == ["发票抬头"], "no floor: coverage alone still passes"
    hits, debug = _search(tenant, question, reranker=reranker, min_rerank=0.5, rerank_floor=0.1)
    assert hits == [] and debug.filtered_low_relevance >= 1
    assert not passes_gate(1.0, 0.0, 0.3, None, rerank=0.03, min_rerank=0.5, rerank_floor=0.1)
    assert passes_gate(1.0, 0.0, 0.3, None, rerank=0.2, min_rerank=0.5, rerank_floor=0.1)
    assert passes_gate(0.0, 0.0, 0.3, None, rerank=0.6, min_rerank=0.5, rerank_floor=0.1)


def test_a_failing_reranker_falls_back_to_the_coverage_gate(tenant):
    hits, debug = _search(tenant, "发票抬头", reranker=BrokenReranker(), min_rerank=0.5, rerank_floor=0.99)
    assert [h.heading for h in hits] == ["发票抬头"], "no veto without scores: coverage decides"
    assert hits[0].rerank is None and "503" in debug.rerank_error
    assert _search(tenant, "邮费谁出", reranker=BrokenReranker(), min_rerank=0.0)[0] == []


def test_only_the_top_candidates_are_reranked(tenant):
    reranker = TableReranker({})
    hits, debug = _search(tenant, "退货运费退款到账发票抬头", min_relevance=0.0, reranker=reranker,
                          rerank_candidates=2)
    assert len(reranker.calls[0][1]) == 2 and debug.reranked == 2
    assert len(hits) <= 2, "what the reranker did not see is not ranked against what it did"


def _http(handler) -> HTTPReranker:
    return HTTPReranker("http://rerank.test/v1", "key", "bge-reranker-v2-m3", transport=httpx.MockTransport(handler))


def test_the_http_reranker_speaks_the_jina_cohere_format_and_tei():
    seen = {}

    def jina(request):
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.91},
                                                     {"index": 0, "relevance_score": 0.08}]})

    assert _http(jina).score("退款多久到账", ["运费说明", "退款到账时间"]) == [0.08, 0.91]
    assert seen["model"] == "bge-reranker-v2-m3" and seen["query"] == "退款多久到账"
    assert seen["documents"] == ["运费说明", "退款到账时间"] and seen["auth"] == "Bearer key"

    def tei(request):  # text-embeddings-inference: a bare list of {index, score}
        return httpx.Response(200, json=[{"index": 0, "score": 0.3}, {"index": 1, "score": 0.7}])

    assert _http(tei).score("q", ["a", "b"]) == [0.3, 0.7]


def test_the_http_reranker_refuses_errors_and_raw_logits():
    with pytest.raises(LLMError, match="503"):
        _http(lambda r: httpx.Response(503, text="busy")).score("q", ["a"])
    logits = lambda r: httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 4.2}]})  # noqa: E731
    with pytest.raises(LLMError, match="0, 1"):
        _http(logits).score("q", ["a"])
    short = lambda r: httpx.Response(200, json={"results": []})  # noqa: E731
    with pytest.raises(LLMError, match="1 passages"):
        _http(short).score("q", ["a"])
    assert _http(lambda r: httpx.Response(500)).score("q", []) == [], "nothing to score: no call"


def test_the_breaker_stops_calling_a_reranker_that_keeps_failing():
    settings = Settings()
    settings.llm_breaker_failures = 2
    inner = BrokenReranker()
    calls = []
    inner.score = lambda q, p: calls.append(q) or (_ for _ in ()).throw(LLMError("rerank HTTP 503"))
    guarded = GuardedReranker(inner, CircuitBreaker(settings), "rerank:x")
    for _ in range(3):
        with pytest.raises(LLMError):
            guarded.score("q", ["a"])
    assert len(calls) == 2, "the third call fails at once, without waiting for the service"


def test_reranking_is_off_by_default_and_configured_from_the_environment(monkeypatch):
    assert Settings().reranker_provider == "none" and build_reranker(Settings()) is None
    monkeypatch.setenv("LOOPDESK_RERANKER_PROVIDER", "http")
    monkeypatch.setenv("LOOPDESK_RERANKER_BASE_URL", "http://tei:8080")
    monkeypatch.setenv("LOOPDESK_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setenv("LOOPDESK_MIN_RERANK", "0.42")
    monkeypatch.setenv("LOOPDESK_RERANK_FLOOR", "0.05")
    monkeypatch.setenv("LOOPDESK_RERANK_CANDIDATES", "10")
    settings = Settings.from_env()
    assert (settings.min_rerank, settings.rerank_floor, settings.rerank_candidates) == (0.42, 0.05, 10)
    reranker = build_reranker(settings)
    assert isinstance(reranker, GuardedReranker) and isinstance(reranker.inner, HTTPReranker)

    monkeypatch.setenv("LOOPDESK_RERANKER_PROVIDER", "local")
    reranker = build_reranker(Settings.from_env())
    assert reranker.name == "local:BAAI/bge-reranker-v2-m3", "built without loading the model"


def test_changing_the_reranker_or_its_gate_invalidates_cached_answers():
    from app.agent.answer_cache import config_fingerprint

    settings = Settings()
    seen = {config_fingerprint(settings)}
    for field, value in (("reranker_provider", "local"), ("reranker_model", "BAAI/bge-reranker-v2-m3"),
                         ("min_rerank", 0.4), ("rerank_floor", 0.05), ("rerank_candidates", 10)):
        setattr(settings, field, value)
        seen.add(config_fingerprint(settings))
    assert len(seen) == 6


def test_the_search_tool_reranks_with_the_configured_reranker(client, ask, login, monkeypatch):
    from app.services import get_services
    from tests.conftest import by_type

    reranker = TableReranker({("退款", "退款"): 0.88})
    monkeypatch.setattr(get_services(), "reranker", reranker)
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    output = [e for e in by_type(events, "tool_result") if e["name"] == "search_knowledge"][-1]["output"]
    assert reranker.calls and output["results"]
    query, passages = reranker.calls[0]
    assert query == "退款多久能到账？", "the customer's words, not the synonym-expanded query"
    assert 1 <= len(passages) <= get_services().settings.rerank_candidates
    assert all("退款" in r["heading"] + r["text"] for r in output["results"])
