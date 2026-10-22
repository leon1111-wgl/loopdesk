"""bge-m3 embeddings and the bge-reranker-v2-m3 cross-encoder, in-process on CPU (W5).

Optional: torch and transformers come from requirements-semantic.txt and are imported on the first
call, never at startup, so the default install and the test jobs stay free of them. One copy of each
model per process, shared by every caller. The evaluation runs these on GitHub Actions
(.github/workflows/retrieval-eval.yml); a production deployment would more often run the same models
behind an HTTP server (Infinity, vLLM, Xinference) and use the openai_compat
embedder and the http reranker instead.
"""

from __future__ import annotations

import threading

from ..llm.base import Embedder, LLMError
from .rerank import Reranker

SETUP_HINT = "pip install -r requirements-semantic.txt (torch CPU + transformers)"
MAX_TOKENS = 512  # a chunk is at most 300 characters plus its heading; longer text is truncated

_lock = threading.Lock()
_loaded: dict[tuple[str, str], tuple] = {}


class ModelSetupError(LLMError):
    """A local model cannot run at all (package missing, wrong size): configuration, not an outage."""


def load(kind: str, name: str, threads: int = 0) -> tuple:
    """(torch, tokenizer, model) for ``kind`` "embed" or "rerank", loaded once per process."""
    try:
        import torch
        from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise ModelSetupError(f"local {kind} model {name}: {SETUP_HINT} ({exc})") from exc
    with _lock:
        if (kind, name) not in _loaded:
            if threads:
                torch.set_num_threads(threads)
            tokenizer = AutoTokenizer.from_pretrained(name)
            cls = AutoModel if kind == "embed" else AutoModelForSequenceClassification
            model = cls.from_pretrained(name)
            model.eval()
            _loaded[(kind, name)] = (torch, tokenizer, model)
        return _loaded[(kind, name)]


class LocalEmbedder(Embedder):
    """bge-m3's dense vector: the [CLS] state of the last layer, L2-normalised (as FlagEmbedding does).
    Queries need no instruction prefix with bge-m3."""

    def __init__(self, model: str, dim: int, threads: int = 0, batch_size: int = 16):
        self.model = model
        self.dim = dim
        self.threads = threads
        self.batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        torch, tokenizer, model = load("embed", self.model, self.threads)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = tokenizer(texts[start:start + self.batch_size], padding=True, truncation=True,
                              max_length=MAX_TOKENS, return_tensors="pt")
            with torch.inference_mode():
                cls = model(**batch).last_hidden_state[:, 0]
                cls = torch.nn.functional.normalize(cls.float(), dim=-1)
            vectors.extend(cls.tolist())
        if vectors and len(vectors[0]) != self.dim:
            raise ModelSetupError(f"embedding model {self.model} returned {len(vectors[0])} dimensions, "
                                  f"LOOPDESK_EMBEDDING_DIM is {self.dim}")
        return vectors


class LocalCrossEncoder(Reranker):
    """bge-reranker-v2-m3: reads the question and a passage together; the sigmoid of its logit is the
    score in [0, 1] (FlagEmbedding's normalize=True)."""

    def __init__(self, model: str, threads: int = 0, batch_size: int = 16):
        self.model = model
        self.name = f"local:{model}"
        self.threads = threads
        self.batch_size = batch_size

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        torch, tokenizer, model = load("rerank", self.model, self.threads)
        scores: list[float] = []
        for start in range(0, len(passages), self.batch_size):
            chunk = passages[start:start + self.batch_size]
            batch = tokenizer([query] * len(chunk), chunk, padding=True, truncation=True,
                              max_length=MAX_TOKENS, return_tensors="pt")
            with torch.inference_mode():
                logits = model(**batch).logits.view(-1).float()
            scores.extend(torch.sigmoid(logits).tolist())
        return scores
