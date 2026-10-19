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
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..llm.base import Embedder
from ..models import Chunk, Document
from ..textutil import canonical_query, expand_query, tokenize

RRF_K = 60
MIN_SIMILARITY = 1e-6  # below this a vector score is rounding noise (float32), not similarity


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
    coverage_tokens: list[str] = field(default_factory=list)
    bm25: list[dict] = field(default_factory=list)
    vector: list[dict] = field(default_factory=list)
    dropped_flagged: int = 0
    filtered_low_relevance: int = 0


class TenantIndex:
    """BM25 over an inverted index (a query only touches the chunks that contain its words) and
    vector scores as one matrix product. The first versions looped over every chunk in Python:
    at 20,000 chunks a search took about 390 ms (python -m app.evals.retrieval_bench)."""

    def __init__(self, chunks: list[IndexedChunk], vectors=None):
        self.chunks = chunks
        self.n = len(chunks)
        self.avgdl = (sum(c.length for c in chunks) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)  # token -> [(chunk, tf)]
        for i, c in enumerate(chunks):
            self.df.update(c.tf.keys())
            for token, tf in c.tf.items():
                self.postings[token].append((i, tf))
        # float32, as stored: 20,000 x 512 is 40 MB, where per-chunk Python lists took several times that
        self.matrix = (np.asarray(vectors, dtype=np.float32).reshape(self.n, -1)
                       if self.n and vectors is not None else None)
        self.visibility = np.array([c.visibility for c in chunks], dtype=object)

    def idf(self, token: str) -> float:
        df = self.df.get(token, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def bm25(self, q_tokens: list[str], allowed, k: int, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for t in set(q_tokens):  # the same token order for every chunk, so sums match a full scan
            postings = self.postings.get(t)
            if not postings:
                continue
            idf = self.idf(t)
            for i, tf in postings:
                c = self.chunks[i]
                if c.visibility not in allowed:
                    continue
                denom = tf + k1 * (1 - b + b * c.length / (self.avgdl or 1))
                scores[i] = scores.get(i, 0.0) + idf * tf * (k1 + 1) / denom
        return sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]

    def vector(self, qvec: list[float], allowed, k: int) -> list[tuple[int, float]]:
        if self.matrix is None or k <= 0:
            return []
        scores = self.matrix @ np.asarray(qvec, dtype=np.float32)
        scores[~np.isin(self.visibility, list(allowed))] = -np.inf
        top = np.argpartition(-scores, k - 1)[:k] if k < self.n else np.arange(self.n)
        ranked = sorted(((int(i), float(scores[i])) for i in top), key=lambda x: (-x[1], x[0]))
        return [s for s in ranked[:k] if s[1] > MIN_SIMILARITY]

    def coverage(self, q_tokens: list[str], c: IndexedChunk) -> float:
        unique = set(q_tokens)
        total = sum(self.idf(t) for t in unique)
        if total == 0:
            return 0.0
        weak = self._weak_parts(unique, c)
        return sum(self.idf(t) for t in unique if t in c.tf and t not in weak) / total

    def _weak_parts(self, unique: set[str], c: IndexedChunk) -> set[str]:
        """Search-mode sub-words of a compound the knowledge base has never seen count only if
        they cover most of it: in "能货到付款吗" the 付款 match ("付款后48小时内发货") says nothing
        about cash on delivery, while a compound spelled out in pieces still counts."""
        weak: set[str] = set()
        for big in unique:
            if len(big) < 3 or self.df.get(big, 0) > 0:
                continue
            parts = [t for t in unique if t != big and t in big]
            covered: set[int] = set()
            for t in parts:
                if t in c.tf:
                    start = big.find(t)
                    covered.update(range(start, start + len(t)))
            if parts and len(covered) < 0.75 * len(big):
                weak.update(parts)
        return weak


class IndexManager:
    """Per-tenant index, rebuilt when the tenant's chunks change. A rebuild reuses the tokens
    of every chunk it has seen before (re-indexing a document creates new chunk rows, so only
    those are tokenized again): tokenizing was most of a rebuild, about 10 s at 50,000 chunks."""

    def __init__(self) -> None:
        self._cache: dict[int, tuple[tuple, TenantIndex]] = {}
        # tenant -> {(chunk id, hash of its text): (term frequencies, length)}. The text is part
        # of the key because SQLite can hand a deleted row's id to a new row.
        self._tokens: dict[int, dict[tuple[int, int], tuple[Counter, int]]] = {}
        self._lock = threading.Lock()

    def signature(self, db: Session, tenant_id: int) -> tuple:
        """Changes whenever the tenant's chunks or document versions change."""
        chunk_stats = db.execute(
            select(func.count(Chunk.id), func.max(Chunk.id)).where(Chunk.tenant_id == tenant_id)
        ).one()
        version_sum = db.execute(
            select(func.coalesce(func.sum(Document.version), 0)).where(Document.tenant_id == tenant_id)
        ).scalar_one()
        return (chunk_stats[0], chunk_stats[1], version_sum)

    def get(self, db: Session, tenant_id: int) -> TenantIndex:
        signature = self.signature(db, tenant_id)
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
        chunks, blobs = [], []
        previous = self._tokens.get(tenant_id, {})
        seen: dict[tuple[int, int], tuple[Counter, int]] = {}
        for chunk, title, visibility in rows:
            text = f"{chunk.heading}\n{chunk.text}"
            key = (chunk.id, hash(text))
            if key not in previous:
                tokens = tokenize(text)
                previous[key] = (Counter(tokens), len(tokens))
            seen[key] = tf, length = previous[key]
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
                    tf=tf,
                    length=length,
                )
            )
            blobs.append(chunk.embedding)
        index = TenantIndex(chunks, _matrix(blobs))
        with self._lock:
            self._cache[tenant_id] = (signature, index)
            self._tokens[tenant_id] = seen  # chunks that no longer exist drop out
        return index

    def invalidate(self, tenant_id: int) -> None:
        with self._lock:
            self._cache.pop(tenant_id, None)


def _matrix(blobs: list[bytes]):
    """Stored float32 vectors straight into one matrix. Rows of another length (the embedding
    model was changed without re-indexing) are left at zero, i.e. never similar."""
    if not blobs:
        return None
    width = max(set(map(len, blobs)), key=[len(b) for b in blobs].count)  # the usual row size
    if all(len(b) == width for b in blobs):
        return np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), -1).copy()
    matrix = np.zeros((len(blobs), width // 4), dtype=np.float32)
    for i, blob in enumerate(blobs):
        if len(blob) == width:
            matrix[i] = np.frombuffer(blob, dtype=np.float32)
    return matrix


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
    # Recall uses the question plus the knowledge base's wording for any colloquial phrase in
    # it; the relevance gate scores the canonical form only (see canonical_query).
    gate_tokens = tokenize(canonical_query(query))
    query = expand_query(query)
    q_tokens = tokenize(query)
    debug = SearchDebug(query_tokens=q_tokens, coverage_tokens=gate_tokens)
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
        cov = index.coverage(gate_tokens or q_tokens, c)
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
