"""Cross-encoder reranking of the fused candidates (W5).

BM25 and the bi-encoder each score the question and a chunk apart; a cross-encoder reads the pair
together, so it can tell "可以用美元付款吗" from "付款后48小时内发货" although they share a word. It is
too slow to score a whole knowledge base, so it reads the top ``rerank_candidates`` after RRF.

Providers: ``local`` (bge-reranker-v2-m3 in-process, app.rag.local_models) and ``http`` (a /rerank
API). The HTTP client sends the Jina / Cohere request that Infinity, vLLM, Xinference and most
vendors serve, and reads their {"results": [{"index", "relevance_score"}]} as well as a bare list of
{"index", "score"}. Scores must be in [0, 1]: the gate's thresholds are calibrated on that scale.
Off by default; when the service fails, a search keeps its RRF order and the coverage gate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from ..config import Settings
from ..llm.base import LLMError
from ..llm.breaker import CircuitBreaker


class Reranker(ABC):
    name: str = "reranker"

    @abstractmethod
    def score(self, query: str, passages: list[str]) -> list[float]:
        """One score in [0, 1] per passage, in the passages' order; higher is more relevant."""


class HTTPReranker(Reranker):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 3.0,
                 transport: httpx.BaseTransport | None = None):
        if not (base_url and model):
            raise LLMError("the http reranker needs LOOPDESK_RERANKER_BASE_URL and LOOPDESK_RERANKER_MODEL")
        self.url = base_url.rstrip("/") + "/rerank"
        self.model = model
        self.name = f"http:{model}"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        response = self.client.post(self.url, json={
            "model": self.model, "query": query, "documents": passages, "top_n": len(passages),
            "return_documents": False,
        })
        if response.status_code >= 400:
            raise LLMError(f"rerank HTTP {response.status_code}: {response.text[:300]}")
        body = response.json()
        rows = body.get("results") if isinstance(body, dict) else body
        scores: dict[int, float] = {}
        for row in rows or []:
            value = row.get("relevance_score", row.get("score"))
            if value is not None:
                scores[int(row["index"])] = float(value)
        if sorted(scores) != list(range(len(passages))):
            raise LLMError(f"reranker {self.model} scored {len(scores)} of {len(passages)} passages")
        if any(not 0.0 <= s <= 1.0 for s in scores.values()):
            raise LLMError(f"reranker {self.model} returned scores outside [0, 1]: the gate needs normalised "
                           "scores (text-embeddings-inference: raw_scores=false)")
        return [scores[i] for i in range(len(passages))]


class GuardedReranker(Reranker):
    """A remote reranker behind the models' circuit breaker: while it is down, searches skip it at once."""

    def __init__(self, inner: Reranker, breaker: CircuitBreaker, key: str):
        self.inner = inner
        self.breaker = breaker
        self.key = key
        self.name = inner.name

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not self.breaker.allow(self.key):
            raise LLMError(f"rerank {self.key}: circuit open")
        try:
            result = self.inner.score(query, passages)
        except Exception:
            self.breaker.record(self.key, False)
            raise
        self.breaker.record(self.key, True)
        return result


def build_reranker(settings: Settings) -> Reranker | None:
    provider = (settings.reranker_provider or "none").strip().lower()
    if provider == "none":
        return None
    if provider not in ("local", "http"):
        raise ValueError("LOOPDESK_RERANKER_PROVIDER must be none, local or http")
    if not settings.reranker_model:
        raise LLMError("a reranker needs LOOPDESK_RERANKER_MODEL (e.g. BAAI/bge-reranker-v2-m3)")
    if provider == "local":
        from .local_models import LocalCrossEncoder

        return LocalCrossEncoder(settings.reranker_model, threads=settings.local_model_threads)
    remote = HTTPReranker(settings.reranker_base_url or "", settings.reranker_api_key or "",
                          settings.reranker_model, timeout=settings.reranker_timeout)
    return GuardedReranker(remote, CircuitBreaker(settings), f"rerank:{settings.reranker_model}")
