"""Hybrid retrieval: BM25 + vector search, fused with Reciprocal Rank Fusion.

Why RRF: BM25 scores and cosine similarities live on different scales, so adding them
needs per-corpus tuning. RRF only uses ranks (score = sum 1 / (60 + rank)), which is
robust and parameter-light.

Why a separate ``relevance`` score: RRF ranks are relative, so they cannot say "nothing
here answers the question". ``relevance`` is the IDF-weighted share of the query's terms
that a chunk covers; when the most specific term (e.g. "海外") is missing, relevance
drops and the agent refuses instead of answering a neighbouring question.

Optional semantic stages (W5): a cross-encoder reranks the top fused candidates, and its score is
the gate's second signal (coverage OR a calibrated rerank score; a calibrated floor can veto a
coverage pass the reranker calls unrelated). Published colloquial questions (app.rag.questions)
are extra index entries of their chunk: they are searched with BM25 and vectors as two more ranked
lists in the RRF, and a chunk's coverage is the best of its own text and its questions.

The index is an in-process cache per tenant, rebuilt when that tenant's chunks change.
It suits up to tens of thousands of chunks; beyond that, move ``VectorIndex`` to
pgvector / Milvus behind the same interface.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..llm.base import Embedder
from ..models import Chunk, Document, IndexQuestion, Tenant
from ..textutil import canonical_query, expand_query, tokenize

RRF_K = 60
logger = logging.getLogger(__name__)
_width_warned: set[tuple[int, int]] = set()
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
    reviewed: bool  # an administrator released it after the scan held it back
    tf: Counter
    length: int


@dataclass
class IndexedQuestion:
    chunk: int  # position in TenantIndex.chunks
    text: str
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
    reviewed: bool = False
    rerank: float | None = None  # the cross-encoder's score, when a reranker read this chunk
    matched_question: str | None = None  # the published question whose coverage counted

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
    vector_error: str | None = None  # the embedding service failed: this search was BM25 only (N-078)
    rerank_error: str | None = None  # the reranker failed: RRF order and the coverage gate only
    reranked: int = 0  # candidates the reranker scored
    questions: list[dict] = field(default_factory=list)  # chunks recalled through a published question


class TenantIndex:
    """BM25 over an inverted index (a query only touches the chunks that contain its words) and
    vector scores as one matrix product. The first versions looped over every chunk in Python:
    at 20,000 chunks a search took about 390 ms (python -m app.evals.retrieval_bench)."""

    def __init__(self, chunks: list[IndexedChunk], vectors=None, questions: list[IndexedQuestion] | None = None,
                 question_vectors=None):
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
        self.pending = 0  # chunks stored without a vector (N-078); a working embedder backfills them
        self.questions = questions or []
        self.stale_questions = 0  # published for a text the section no longer has
        self.by_chunk: dict[int, list[int]] = defaultdict(list)
        self.q_df: Counter = Counter()
        self.q_postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for j, q in enumerate(self.questions):
            self.by_chunk[q.chunk].append(j)
            self.q_df.update(q.tf.keys())
            for token, tf in q.tf.items():
                self.q_postings[token].append((j, tf))
        self.q_avgdl = (sum(q.length for q in self.questions) / len(self.questions)) if self.questions else 0.0
        self.q_matrix = (np.asarray(question_vectors, dtype=np.float32).reshape(len(self.questions), -1)
                         if self.questions and question_vectors is not None else None)

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
        if self.matrix.shape[1] != len(qvec):
            # The embedding model changed and the documents were not re-indexed yet: the product
            # is undefined, and old and new vectors are not comparable anyway. BM25 still answers.
            if (self.matrix.shape[1], len(qvec)) not in _width_warned:
                _width_warned.add((self.matrix.shape[1], len(qvec)))
                logger.warning("stored vectors have %d dimensions, the query has %d: re-index the documents",
                               self.matrix.shape[1], len(qvec))
            return []
        scores = self.matrix @ np.asarray(qvec, dtype=np.float32)
        scores[~np.isin(self.visibility, list(allowed))] = -np.inf
        top = np.argpartition(-scores, k - 1)[:k] if k < self.n else np.arange(self.n)
        ranked = sorted(((int(i), float(scores[i])) for i in top), key=lambda x: (-x[1], x[0]))
        return [s for s in ranked[:k] if s[1] > MIN_SIMILARITY]

    def question_bm25(self, q_tokens: list[str], allowed, k: int, k1: float = 1.5, b: float = 0.75
                      ) -> list[tuple[int, float]]:
        """BM25 over the published questions (their own document frequencies); a chunk ranks by its
        best question."""
        if not self.questions:
            return []
        n = len(self.questions)
        scores: dict[int, float] = {}
        for t in set(q_tokens):
            postings = self.q_postings.get(t)
            if not postings:
                continue
            df = self.q_df[t]
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for j, tf in postings:
                q = self.questions[j]
                if self.chunks[q.chunk].visibility not in allowed:
                    continue
                denom = tf + k1 * (1 - b + b * q.length / (self.q_avgdl or 1))
                scores[j] = scores.get(j, 0.0) + idf * tf * (k1 + 1) / denom
        return self._best_per_chunk(scores, k)

    def question_vector(self, qvec: list[float], allowed, k: int) -> list[tuple[int, float]]:
        if self.q_matrix is None or k <= 0 or self.q_matrix.shape[1] != len(qvec):
            return []
        sims = self.q_matrix @ np.asarray(qvec, dtype=np.float32)
        scores = {j: float(v) for j, v in enumerate(sims)
                  if v > MIN_SIMILARITY and self.chunks[self.questions[j].chunk].visibility in allowed}
        return self._best_per_chunk(scores, k)

    def _best_per_chunk(self, scores: dict[int, float], k: int) -> list[tuple[int, float]]:
        best: dict[int, float] = {}
        for j, score in scores.items():
            chunk = self.questions[j].chunk
            if score > best.get(chunk, float("-inf")):
                best[chunk] = score
        return sorted(best.items(), key=lambda x: (-x[1], x[0]))[:k]

    def question_coverage(self, q_tokens: list[str], chunk: int) -> tuple[float, str | None]:
        """The best coverage of the question by one of the chunk's published questions, and which."""
        best, text = 0.0, None
        for j in self.by_chunk.get(chunk, ()):
            cov = self.coverage(q_tokens, self.questions[j])
            if cov > best:
                best, text = cov, self.questions[j].text
        return best, text

    def known_parts(self, tokens: list[str]) -> list[str]:
        """A query word the knowledge base never uses, read as the KB words inside it (longest
        first) when what is left over is single characters: jieba glues a character onto a known
        word in short questions ("怎么升金卡" -> 升金卡, "金卡有什么好处" -> 金卡有) while the KB says
        金卡 (N-029). A leftover of two or more characters ("货到付款" -> 货到) is a concept the KB
        does not cover, so the word stays as it is and keeps its weight in the coverage."""
        out: list[str] = []
        for t in tokens:
            if len(t) < 3 or self.df.get(t, 0) > 0:
                out.append(t)
                continue
            parts: list[str] = []
            leftovers: list[str] = []
            run, i = "", 0
            while i < len(t):
                for j in range(len(t), i + 1, -1):
                    if self.df.get(t[i:j], 0) > 0:
                        parts.append(t[i:j])
                        if run:
                            leftovers.append(run)
                        run, i = "", j
                        break
                else:
                    run += t[i]
                    i += 1
            if run:
                leftovers.append(run)
            out.extend(parts if parts and all(len(x) == 1 for x in leftovers) else [t])
        return out

    def coverage(self, q_tokens: list[str], c: IndexedChunk | IndexedQuestion) -> float:
        unique = set(q_tokens)
        total = sum(self.idf(t) for t in unique)
        if total == 0:
            return 0.0
        weak = self._weak_parts(unique, c)
        return sum(self.idf(t) for t in unique if t in c.tf and t not in weak) / total

    def _weak_parts(self, unique: set[str], c: IndexedChunk | IndexedQuestion) -> set[str]:
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
        """Changes whenever the tenant's chunks change: one primary-key lookup. It used to count
        the chunks and sum the document versions on every search, 2.6 ms at 50,000 chunks
        (SQLite) and two more round trips on PostgreSQL, twice per chat request (here and in
        the answer cache key)."""
        return (db.execute(select(Tenant.kb_version).where(Tenant.id == tenant_id)).scalar_one(),)

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
                    reviewed=chunk.reviewed_at is not None,
                    tf=tf,
                    length=length,
                )
            )
            blobs.append(chunk.embedding)
        questions, question_blobs, stale = self._questions(db, tenant_id, chunks, previous, seen)
        index = TenantIndex(chunks, _matrix(blobs), questions, _matrix(question_blobs))
        index.pending = sum(1 for b in blobs if not b) + sum(1 for b in question_blobs if not b)
        index.stale_questions = stale
        with self._lock:
            self._cache[tenant_id] = (signature, index)
            self._tokens[tenant_id] = seen  # chunks that no longer exist drop out
        return index

    @staticmethod
    def _questions(db: Session, tenant_id: int, chunks: list[IndexedChunk], previous: dict, seen: dict
                   ) -> tuple[list[IndexedQuestion], list[bytes], int]:
        """The published questions whose chunk still has the exact text they were approved for."""
        rows = db.execute(select(IndexQuestion).where(IndexQuestion.tenant_id == tenant_id)
                          .order_by(IndexQuestion.id)).scalars().all()
        if not rows:
            return [], [], 0
        where: dict[tuple[int, str, str], int] = {}
        for i, c in enumerate(chunks):
            where.setdefault((c.document_id, c.heading, text_sha(c.text)), i)
        questions, blobs, stale = [], [], 0
        for row in rows:
            i = where.get((row.document_id, row.heading, row.text_sha))
            if i is None:
                stale += 1
                continue
            key = (-row.id, hash(row.question))  # negative ids: never a chunk's key
            if key not in previous:
                tokens = tokenize(row.question)
                previous[key] = (Counter(tokens), len(tokens))
            seen[key] = tf, length = previous[key]
            questions.append(IndexedQuestion(chunk=i, text=row.question, tf=tf, length=length))
            blobs.append(row.embedding)
        return questions, blobs, stale

    def invalidate(self, tenant_id: int) -> None:
        with self._lock:
            self._cache.pop(tenant_id, None)

    def clear(self) -> None:
        """Forget every tenant (a tool that switches databases: tenant ids repeat across them)."""
        with self._lock:
            self._cache.clear()
            self._tokens.clear()


def text_sha(text: str) -> str:
    """What a published question is bound to: the exact text of the chunk it was approved for."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _matrix(blobs: list[bytes]):
    """Stored float32 vectors straight into one matrix. Rows of another length (the embedding
    model was changed without re-indexing) are left at zero, i.e. never similar."""
    if not blobs:
        return None
    widths = [len(b) for b in blobs if b]  # empty: stored while the embedding service was down (N-078)
    if not widths:
        return None
    width = max(set(widths), key=widths.count)  # the usual row size
    if all(len(b) == width for b in blobs):
        return np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), -1).copy()
    matrix = np.zeros((len(blobs), width // 4), dtype=np.float32)
    for i, blob in enumerate(blobs):
        if len(blob) == width:
            matrix[i] = np.frombuffer(blob, dtype=np.float32)
    return matrix


index_manager = IndexManager()


class Prewarm:
    """Builds every tenant's index once at startup, in the background: otherwise the first
    question after a restart waits for it (about 9 s at 50,000 chunks). The readiness probe
    reports not ready until this is done, so a load balancer sends no traffic before."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: str | None = None

    def start(self, enabled: bool) -> None:
        self.done.clear()
        self.error = None
        if not enabled:
            self.done.set()
            return
        threading.Thread(target=self._run, name="index-prewarm", daemon=True).start()

    def _run(self) -> None:
        from ..db import new_session

        try:
            with new_session() as db:
                for tenant_id in db.execute(select(Tenant.id)).scalars().all():
                    index_manager.get(db, tenant_id)
        except Exception as exc:  # pragma: no cover - a search builds the index anyway
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done.set()


prewarm = Prewarm()


class Backfill:
    """Embeds the chunks stored without vectors (N-078) in the background, one run at a time and at
    most once per ``interval`` seconds, so a search never waits for it."""

    def __init__(self, interval: float = 30.0) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._running = False
        self._last = float("-inf")
        self.filled = 0

    def kick(self, embedder: Embedder, wait: bool = False) -> None:
        with self._lock:
            if self._running or time.monotonic() - self._last < self.interval:
                return
            self._running = True
            self._last = time.monotonic()
        thread = threading.Thread(target=self._run, args=(embedder,), name="embed-backfill", daemon=True)
        thread.start()
        if wait:
            thread.join()

    def _run(self, embedder: Embedder) -> None:
        from ..db import new_session
        from .ingest import embed_pending

        try:
            with new_session() as db:
                self.filled += embed_pending(db, embedder)
        except Exception:  # pragma: no cover - the next search kicks it again
            logger.exception("embedding backfill failed")
        finally:
            with self._lock:
                self._running = False


backfill = Backfill()


def cosine_gate(settings) -> float | None:
    """The cosine a chunk needs to pass on vector similarity alone; None: it never does (N-030).
    The offline hashing embedder's cosine is lexical, so it shares the coverage threshold. A
    semantic model's cosine is high even for unrelated text until a threshold is calibrated."""
    if settings.min_cosine is not None:
        return settings.min_cosine
    return settings.min_relevance if settings.embedding_provider == "hashing" else None


def gate_options(settings) -> dict:
    """hybrid_search's gate arguments as configured (the reranker itself comes from Services)."""
    return {"min_relevance": settings.min_relevance, "min_cosine": cosine_gate(settings),
            "min_rerank": settings.min_rerank, "rerank_floor": settings.rerank_floor,
            "rerank_candidates": settings.rerank_candidates, "index_questions": settings.index_questions}


def passes_gate(coverage: float, cosine: float, min_relevance: float, min_cosine: float | None,
                rerank: float | None = None, min_rerank: float | None = None,
                rerank_floor: float | None = None) -> bool:
    """Two signals (W5): term coverage (or a calibrated cosine) OR a calibrated rerank score. A chunk
    that passes on coverage is vetoed when the reranker scored it below ``rerank_floor``. Without a
    rerank score (no reranker, or it failed) the lexical side decides alone."""
    lexical = coverage >= min_relevance or (min_cosine is not None and cosine >= min_cosine)
    if rerank is None:
        return lexical
    if min_rerank is not None and rerank >= min_rerank:
        return True
    return lexical and (rerank_floor is None or rerank >= rerank_floor)


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
    min_cosine: float | None = None,
    reranker=None,
    min_rerank: float | None = None,
    rerank_floor: float | None = None,
    rerank_candidates: int = 20,
    index_questions: bool = True,
) -> tuple[list[Hit], SearchDebug]:
    """``min_relevance`` gates the term coverage, ``min_cosine`` the vector similarity (see
    cosine_gate; None: the cosine alone never lets a chunk through). With a ``reranker``
    (app.rag.rerank), the top ``rerank_candidates`` fused chunks are reordered by its score, and
    ``min_rerank`` / ``rerank_floor`` are its side of the gate (see passes_gate; None: it only
    reorders). ``index_questions``: published colloquial questions count as index entries."""
    index = index_manager.get(db, tenant_id)
    asked = query
    # Recall uses the question plus the knowledge base's wording for any colloquial phrase in
    # it; the relevance gate scores the canonical form only (see canonical_query).
    gate_tokens = index.known_parts(tokenize(canonical_query(query)))
    query = expand_query(query)
    q_tokens = index.known_parts(tokenize(query))
    debug = SearchDebug(query_tokens=q_tokens, coverage_tokens=gate_tokens)
    if index.n == 0:
        return [], debug

    # The embedding service is a separate dependency: when it fails, BM25 and the coverage gate
    # still work on their own, so the search degrades to them instead of failing (N-078). The
    # caller tells the customer the search was partial rather than "the knowledge base has nothing".
    try:
        qvec = embedder.embed_query(query)
    except Exception as exc:
        qvec = None
        debug.vector_error = f"{type(exc).__name__}: {exc}"[:200]
        logger.warning("embedding failed, searching with BM25 only: %s", debug.vector_error)
    if qvec is not None and index.pending:
        backfill.kick(embedder)  # the service answers again: embed what was stored without vectors
    bm25 = index.bm25(q_tokens, visibility, candidate_k)
    vec = index.vector(qvec, visibility, candidate_k) if qvec is not None else []
    debug.bm25 = [{"chunk_id": index.chunks[i].chunk_id, "score": round(s, 4)} for i, s in bm25]
    debug.vector = [{"chunk_id": index.chunks[i].chunk_id, "score": round(s, 4)} for i, s in vec]
    lists = [bm25, vec]
    use_questions = index_questions and bool(index.questions)
    if use_questions:  # two more ranked lists: the published questions, by BM25 and by vector
        q_bm25 = index.question_bm25(q_tokens, visibility, candidate_k)
        q_vec = index.question_vector(qvec, visibility, candidate_k) if qvec is not None else []
        lists += [q_bm25, q_vec]
        debug.questions = [{"chunk_id": index.chunks[i].chunk_id, "via": via, "score": round(s, 4)}
                           for via, found in (("bm25", q_bm25), ("vector", q_vec)) for i, s in found]

    fused: dict[int, float] = {}
    ranks = [{i: r for r, (i, _) in enumerate(found, start=1)} for found in lists]
    bm25_rank, vec_rank = ranks[0], ranks[1]
    cosine = dict(vec)
    if use_questions:
        for i, score in q_vec:  # a chunk is as similar as its most similar entry
            cosine[i] = max(cosine.get(i, 0.0), score)
    for rank_of in ranks:
        for i, rank in rank_of.items():
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)

    candidates: list[tuple[int, float]] = []
    for i, score in sorted(fused.items(), key=lambda x: -x[1]):
        if index.chunks[i].flagged and not include_flagged:
            debug.dropped_flagged += 1
            continue
        candidates.append((i, score))
    rerank: dict[int, float] = {}
    if reranker is not None and candidates:
        # The cross-encoder reads the top candidates only, and what it did not read is not ranked
        # against what it did. The customer's own words: the synonym expansion is for BM25.
        candidates = candidates[:max(1, rerank_candidates)]
        try:
            scores = reranker.score(asked, [f"{index.chunks[i].heading}\n{index.chunks[i].text}" for i, _ in candidates])
            if len(scores) != len(candidates):
                raise ValueError(f"{len(scores)} scores for {len(candidates)} passages")
            rerank = {i: float(v) for (i, _), v in zip(candidates, scores)}
            debug.reranked = len(candidates)
            candidates.sort(key=lambda x: -rerank[x[0]])  # stable: ties keep the RRF order
        except Exception as exc:  # the coverage gate still works without it
            debug.rerank_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("rerank failed, keeping the fused order: %s", debug.rerank_error)

    hits: list[Hit] = []
    for i, score in candidates:
        c = index.chunks[i]
        cov = index.coverage(gate_tokens or q_tokens, c)
        matched = None
        if use_questions:
            by_question, question = index.question_coverage(gate_tokens or q_tokens, i)
            if by_question > cov:
                cov, matched = by_question, question
        cos = cosine.get(i, 0.0)
        rr = rerank.get(i)
        if not passes_gate(cov, cos, min_relevance, min_cosine, rr, min_rerank, rerank_floor):
            debug.filtered_low_relevance += 1
            continue
        # A semantic score counts towards relevance only where it can pass the gate on its own.
        relevance = max(cov, cos) if min_cosine is not None else cov
        if rr is not None and min_rerank is not None:
            relevance = max(relevance, rr)
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
                reviewed=c.reviewed,
                rerank=rr,
                matched_question=matched,
            )
        )
        if len(hits) == top_k:
            break
    return hits, debug
