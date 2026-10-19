"""Embedders.

``HashingEmbedder`` is an offline, deterministic baseline (feature hashing of words and
character bigrams). It keeps the demo and CI free of model downloads. Production should
use a semantic model through ``OpenAICompatEmbedder`` (e.g. doubao-embedding,
hunyuan-embedding, bge-m3), configured with LOOPDESK_EMBEDDING_*.
"""

from __future__ import annotations

import hashlib
import math
from array import array
from collections import Counter

from ..config import Settings
from ..llm.base import Embedder
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


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai_compat":
        from ..llm.openai_compat import OpenAICompatEmbedder

        return OpenAICompatEmbedder(
            settings.embedding_base_url or "",
            settings.embedding_api_key or "",
            settings.embedding_model or "",
            settings.embedding_dim,
        )
    return HashingEmbedder(settings.embedding_dim)
