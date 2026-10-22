"""In-process bge-m3 and bge-reranker-v2-m3 (LOOPDESK_EMBEDDING_PROVIDER=local, LOOPDESK_RERANKER_PROVIDER=local).

torch and transformers are optional (requirements-semantic.txt): the test jobs and the default
install never load them, and building the providers does not import them. A missing package is a
configuration error, raised at once, not an outage to wait out (ingest would otherwise store every
chunk without a vector and retry forever)."""

import builtins
import sys

import pytest

from app.config import Settings
from app.llm.base import LLMError
from app.rag.embeddings import build_embedder
from app.rag.ingest import transient
from app.rag.rerank import build_reranker


def _settings(**values) -> Settings:
    settings = Settings()
    for key, value in values.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture()
def no_torch(monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in ("torch", "transformers"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    for name in [m for m in sys.modules if m.split(".")[0] in ("torch", "transformers")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(builtins, "__import__", guarded)


def test_local_providers_are_built_without_loading_a_model(no_torch):
    embedder = build_embedder(_settings(embedding_provider="local", embedding_model="BAAI/bge-m3", embedding_dim=1024))
    assert embedder.dim == 1024 and embedder.model == "BAAI/bge-m3"
    reranker = build_reranker(_settings(reranker_provider="local", reranker_model="BAAI/bge-reranker-v2-m3"))
    assert reranker.name == "local:BAAI/bge-reranker-v2-m3"


def test_a_missing_package_is_a_configuration_error(no_torch):
    embedder = build_embedder(_settings(embedding_provider="local", embedding_model="BAAI/bge-m3", embedding_dim=1024))
    with pytest.raises(LLMError, match="requirements-semantic.txt") as raised:
        embedder.embed(["退款多久到账"])
    assert not transient(raised.value), "ingest must fail loudly, not store chunks without vectors"
    reranker = build_reranker(_settings(reranker_provider="local", reranker_model="BAAI/bge-reranker-v2-m3"))
    with pytest.raises(LLMError, match="requirements-semantic.txt"):
        reranker.score("退款多久到账", ["退款审核通过后1-3个工作日到账"])


def test_a_local_embedder_needs_a_model_name():
    with pytest.raises(LLMError, match="LOOPDESK_EMBEDDING_MODEL"):
        build_embedder(_settings(embedding_provider="local", embedding_model=None))
    with pytest.raises(LLMError, match="LOOPDESK_RERANKER_MODEL"):
        build_reranker(_settings(reranker_provider="local", reranker_model=None))
    with pytest.raises(ValueError, match="none, local or http"):
        build_reranker(_settings(reranker_provider="cohere"))
