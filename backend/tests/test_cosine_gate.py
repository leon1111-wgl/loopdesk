"""The vector path's relevance gate (review round 4, N-030). Relevance was max(coverage, cosine)
against one threshold, 0.3. A semantic embedding model gives unrelated sentences a cosine around
0.6 (its vectors share a common direction), so with one plugged in every chunk passed, even for
"你们CEO是谁". Its vectors were not normalised either (a "cosine" of 1031) and their length was
never checked against LOOPDESK_EMBEDDING_DIM."""

import math

import httpx
import pytest

from app.config import Settings
from app.db import new_session
from app.llm.base import LLMError
from app.models import Tenant
from app.rag.retrieval import TenantIndex, cosine_gate, hybrid_search
from app.services import get_services

OUT_OF_SCOPE = "你们CEO是谁"
IN_SCOPE = "退款多久能到账？"


@pytest.fixture()
def anisotropic(monkeypatch):
    """Every chunk at cosine 0.61 to every question, like an uncalibrated semantic model."""
    def vector(self, qvec, allowed, k):
        return [(i, 0.61) for i in range(self.n) if self.chunks[i].visibility in allowed][:k]

    monkeypatch.setattr(TenantIndex, "vector", vector)


def _search(question: str, settings: Settings):
    services = get_services()
    with new_session() as db:
        tenant = db.query(Tenant).filter(Tenant.slug == "leon").one()
        hits, _ = hybrid_search(db, tenant.id, question, services.embedder, {"public"},
                                min_relevance=settings.min_relevance, min_cosine=cosine_gate(settings))
    return hits


def test_an_uncalibrated_semantic_cosine_never_opens_the_gate(client, settings, anisotropic):
    settings.embedding_provider = "openai_compat"  # a semantic model, and no LOOPDESK_MIN_COSINE yet
    assert cosine_gate(settings) is None
    assert _search(OUT_OF_SCOPE, settings) == [], "0.61 everywhere is not evidence of relevance"
    hits = _search(IN_SCOPE, settings)
    assert hits and all(h.coverage >= settings.min_relevance for h in hits), "coverage still lets real matches in"

    settings.min_cosine = 0.7  # calibrated for this model (gate_probe --calibrate)
    assert _search(OUT_OF_SCOPE, settings) == []
    settings.min_cosine = 0.6
    assert _search(OUT_OF_SCOPE, settings), "a calibrated threshold below the model's floor lets it in"


def test_the_offline_hashing_embedder_keeps_its_gate(settings):
    # Its cosine is lexical (hashed words and bigrams), so it keeps sharing min_relevance: the
    # offline evaluation behaves exactly as before.
    assert settings.embedding_provider == "hashing" and cosine_gate(settings) == settings.min_relevance


def _embedder(vectors: list[list[float]], dim: int):
    from app.llm.openai_compat import OpenAICompatEmbedder

    def reply(request):
        return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})

    return OpenAICompatEmbedder("http://embed.test/v1", "key", "hunyuan-embedding", dim,
                                transport=httpx.MockTransport(reply))


def test_remote_vectors_are_normalised_and_their_size_checked():
    [vector] = _embedder([[30.0, 40.0, 0.0]], dim=3).embed(["退款多久能到账？"])
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)
    with pytest.raises(LLMError, match="1024.*512"):
        _embedder([[0.1] * 1024], dim=512).embed(["退款多久能到账？"])


def test_changing_the_cosine_gate_invalidates_cached_answers():
    # The gate decides what the agent may answer from, so cached answers must not outlive it.
    from app.agent.answer_cache import config_fingerprint

    settings = Settings()
    before = config_fingerprint(settings)
    settings.min_cosine = 0.8
    assert config_fingerprint(settings) != before
