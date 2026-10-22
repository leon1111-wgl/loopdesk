"""Process-wide singletons (model provider, embedder, reranker, tool registry, order and payment systems)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .agent.business_tools import build_registry
from .agent.runtime import AgentRuntime
from .agent.tools import ToolRegistry
from .config import Settings, get_settings
from .integrations import Commerce
from .llm import build_provider
from .llm.base import Embedder, LLMProvider
from .llm.breaker import CircuitBreaker
from .rag.embeddings import build_embedder
from .rag.rerank import Reranker, build_reranker


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider: LLMProvider = build_provider(settings)
        self.embedder: Embedder = build_embedder(settings)
        self.reranker: Reranker | None = build_reranker(settings)  # None unless LOOPDESK_RERANKER_PROVIDER is set
        self.registry: ToolRegistry = build_registry()
        self.breaker = CircuitBreaker(settings)
        self.commerce = Commerce(settings)  # the order and payment systems, per tenant

    def runtime(self, db: Session, commerce: Commerce | None = None) -> AgentRuntime:
        return AgentRuntime(db, self.settings, self.provider, self.embedder, self.registry, self.breaker,
                            commerce or self.commerce, reranker=self.reranker)


_services: Services | None = None


def get_services() -> Services:
    global _services
    if _services is None:
        _services = Services(get_settings())
    return _services


def set_services(services: Services) -> None:
    global _services
    _services = services
