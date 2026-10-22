"""Embedders.

``HashingEmbedder`` is an offline, deterministic baseline (feature hashing of words and
character bigrams). It keeps the demo and CI free of model downloads. Production should
use a semantic model through ``OpenAICompatEmbedder`` (e.g. doubao-embedding,
hunyuan-embedding, bge-m3 behind Infinity or vLLM), configured with LOOPDESK_EMBEDDING_*, or bge-m3
in-process (``local``, app.rag.local_models).
"""

from __future__ import annotations

import hashlib
import math
from array import array
from collections import Counter

from ..config import Settings
from ..llm.base import Embedder, LLMError
from ..llm.breaker import CircuitBreaker
from ..textutil import tokenize


class HashingEmbedder(Embedder):
    def __init__(self, dim: int = 512):
        self.dim = dim

    def _features(self, text: str) -> Counter:
        features: Counter = Counter()
        for token in tokenize(text):
            features[f"w:{token}"] += 1.0
        compact = "".join(ch for ch in text if "一" <= ch <= "鿿")
        for i in range(len(compact) - 1):
            features[f"c:{compact[i:i + 2]}"] += 0.5
        return features

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for feature, tf in self._features(text).items():
                digest = int(hashlib.md5(feature.encode()).hexdigest()[:8], 16)
                sign = 1.0 if digest & 1 else -1.0
                vec[(digest >> 1) % self.dim] += sign * (1.0 + math.log(tf + 1.0))
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


def pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return values.tolist()


class GuardedEmbedder(Embedder):
    """A remote embedder behind the same circuit breaker as the models (N-078): while the service
    is down, a search fails over to BM25 at once instead of waiting out a timeout per question."""

    def __init__(self, inner: Embedder, breaker: CircuitBreaker, key: str):
        self.inner = inner
        self.breaker = breaker
        self.key = key
        self.dim = inner.dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._guarded(lambda: self.inner.embed(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._guarded(lambda: self.inner.embed_query(text))

    def _guarded(self, call):
        if not self.breaker.allow(self.key):
            raise LLMError(f"embedding {self.key}: circuit open")
        try:
            result = call()
        except Exception:
            self.breaker.record(self.key, False)
            raise
        self.breaker.record(self.key, True)
        return result


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "local":
        from .local_models import LocalEmbedder

        if not settings.embedding_model:
            raise LLMError("local embeddings need LOOPDESK_EMBEDDING_MODEL (e.g. BAAI/bge-m3, LOOPDESK_EMBEDDING_DIM=1024)")
        return LocalEmbedder(settings.embedding_model, settings.embedding_dim, threads=settings.local_model_threads)
    if settings.embedding_provider == "openai_compat":
        from ..llm.openai_compat import OpenAICompatEmbedder

        remote = OpenAICompatEmbedder(
            settings.embedding_base_url or "",
            settings.embedding_api_key or "",
            settings.embedding_model or "",
            settings.embedding_dim,
            query_timeout=settings.embedding_timeout,
        )
        return GuardedEmbedder(remote, CircuitBreaker(settings), f"embedding:{settings.embedding_model}")
    return HashingEmbedder(settings.embedding_dim)
