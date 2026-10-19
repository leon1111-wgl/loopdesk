"""Hybrid retrieval: BM25 + vector search, fused with Reciprocal Rank Fusion.

Why RRF: BM25 scores and cosine similarities live on different scales, so adding them
needs per-corpus tuning. RRF only uses ranks (score = sum 1 / (60 + rank)), which is
robust and parameter-light.

Why a separate ``relevance`` score: RRF ranks are relative, so they cannot say "nothing
here answers the question". ``relevance`` is the IDF-weighted share of the query's terms
that a chunk covers; when the most specific term (e.g. "海外") is missing, relevance
drops and the agent refuses instead of answering a neighbouring question.

The index is an in-process cache per tenant, rebuilt when that tenant's chunks change.
It suits up to tens of thousands of chunks; beyond that, move ``VectorIndex`` to
pgvector / Milvus behind the same interface.
"""

from __future__ import annotations

import math
import threading
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..llm.base import Embedder
from ..models import Chunk, Document
from ..textutil import tokenize
from .embeddings import unpack

RRF_K = 60


@dataclass
class IndexedChunk:
    chunk_id: int
    document_id: int
    doc_title: str
    heading: str
    text: str
    page: int | None
    visibility: str
    flagged: bool
    tf: Counter
    length: int
    vector: list[float]


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    doc_title: str
    heading: str
    text: str
    page: int | None
    visibility: str
    rrf: float
    relevance: float
    coverage: float
    cosine: float
    bm25_rank: int | None
    vector_rank: int | None

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class SearchDebug:
    query_tokens: list[str]
    bm25: list[dict] = field(default_factory=list)
    vector: list[dict] = field(default_factory=list)
    dropped_flagged: int = 0
    filtered_low_relevance: int = 0


class TenantIndex:
    def __init__(self, chunks: list[IndexedChunk]):
        self.chunks = chunks
        self.n = len(chunks)
        self.avgdl = (sum(c.length for c in chunks) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for c in chunks:
            self.df.update(c.tf.keys())

    def idf(self, token: str) -> float:
        df = self.df.get(token, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def bm25(self, q_tokens: list[str], allowed, k: int, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
        scores: list[tuple[int, float]] = []
        unique = set(q_tokens)
        for i, c in enumerate(self.chunks):
            if c.visibility not in allowed:
                continue
            score = 0.0
            for t in unique:
                tf = c.tf.get(t)
                if tf:
                    denom = tf + k1 * (1 - b + b * c.length / (self.avgdl or 1))
                    score += self.idf(t) * tf * (k1 + 1) / denom
            if score > 0:
                scores.append((i, score))
        scores.sort(key=lambda x: -x[1])
        return scores[:k]

    def vector(self, qvec: list[float], allowed, k: int) -> list[tuple[int, float]]:
        scores = [
            (i, sum(a * b for a, b in zip(qvec, c.vector)))
            for i, c in enumerate(self.chunks)
            if c.visibility in allowed
        ]
        scores.sort(key=lambda x: -x[1])
        return [s for s in scores[:k] if s[1] > 0]

    def coverage(self, q_tokens: list[str], c: IndexedChunk) -> float:
        unique = set(q_tokens)
        total = sum(self.idf(t) for t in unique)
        if total == 0:
            return 0.0
        return sum(self.idf(t) for t in unique if t in c.tf) / total


class IndexManager:
    def __init__(self) -> None:
        self._cache: dict[int, tuple[tuple, TenantIndex]] = {}
        self._lock = threading.Lock()

    def _signature(self, db: Session, tenant_id: int) -> tuple:
        chunk_stats = db.execute(
            select(func.count(Chunk.id), func.max(Chunk.id)).where(Chunk.tenant_id == tenant_id)
        ).one()
        version_sum = db.execute(
            select(func.coalesce(func.sum(Document.version), 0)).where(Document.tenant_id == tenant_id)
        ).scalar_one()
        return (chunk_stats[0], chunk_stats[1], version_sum)

    def get(self, db: Session, tenant_id: int) -> TenantIndex:
        signature = self._signature(db, tenant_id)
        with self._lock:
            cached = self._cache.get(tenant_id)
            if cached and cached[0] == signature:
                return cached[1]
        rows = db.execute(
            select(Chunk, Document.title, Document.visibility)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.tenant_id == tenant_id)
            .order_by(Chunk.id)
        ).all()
        chunks = []
        for chunk, title, visibility in rows:
            tokens = tokenize(f"{chunk.heading}\n{chunk.text}")
            chunks.append(
                IndexedChunk(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    doc_title=title,
                    heading=chunk.heading,
                    text=chunk.text,
                    page=chunk.page,
                    visibility=visibility,
                    flagged=chunk.flagged_injection,
                    tf=Counter(tokens),
                    length=len(tokens),
                    vector=unpack(chunk.embedding),
                )
            )
        index = TenantIndex(chunks)
        with self._lock:
            self._cache[tenant_id] = (signature, index)
        return index

    def invalidate(self, tenant_id: int) -> None:
        with self._lock:
            self._cache.pop(tenant_id, None)


index_manager = IndexManager()


def hybrid_search(
    db: Session,
    tenant_id: int,
    query: str,
    embedder: Embedder,
    visibility: set[str],
    top_k: int = 4,
    candidate_k: int = 12,
    min_relevance: float = 0.0,
    include_flagged: bool = False,
) -> tuple[list[Hit], SearchDebug]:
    index = index_manager.get(db, tenant_id)
    q_tokens = tokenize(query)
    debug = SearchDebug(query_tokens=q_tokens)
    if index.n == 0:
        return [], debug

    qvec = embedder.embed([query])[0]
    bm25 = index.bm25(q_tokens, visibility, candidate_k)
    vec = index.vector(qvec, visibility, candidate_k)
    debug.bm25 = [{"chunk_id": index.chunks[i].chunk_id, "score": round(s, 4)} for i, s in bm25]
    debug.vector = [{"chunk_id": index.chunks[i].chunk_id, "score": round(s, 4)} for i, s in vec]

    fused: dict[int, float] = {}
    bm25_rank = {i: r for r, (i, _) in enumerate(bm25, start=1)}
    vec_rank = {i: r for r, (i, _) in enumerate(vec, start=1)}
    cosine = dict(vec)
    for ranks in (bm25_rank, vec_rank):
        for i, rank in ranks.items():
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)

    hits: list[Hit] = []
    for i, score in sorted(fused.items(), key=lambda x: -x[1]):
        c = index.chunks[i]
        if c.flagged and not include_flagged:
            debug.dropped_flagged += 1
            continue
        cov = index.coverage(q_tokens, c)
        cos = cosine.get(i, 0.0)
        relevance = max(cov, cos)
        if relevance < min_relevance:
            debug.filtered_low_relevance += 1
            continue
        hits.append(
            Hit(
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                doc_title=c.doc_title,
                heading=c.heading,
                text=c.text,
                page=c.page,
                visibility=c.visibility,
                rrf=score,
                relevance=relevance,
                coverage=cov,
                cosine=cos,
                bm25_rank=bm25_rank.get(i),
                vector_rank=vec_rank.get(i),
            )
        )
        if len(hits) == top_k:
            break
    return hits, debug
