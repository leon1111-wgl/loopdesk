"""Process-wide singletons (model provider, embedder, tool registry)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .agent.business_tools import build_registry
from .agent.runtime import AgentRuntime
from .agent.tools import ToolRegistry
from .config import Settings, get_settings
from .llm import build_provider
from .llm.base import Embedder, LLMProvider
from .llm.breaker import CircuitBreaker
from .rag.embeddings import build_embedder


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider: LLMProvider = build_provider(settings)
        self.embedder: Embedder = build_embedder(settings)
        self.registry: ToolRegistry = build_registry()
        self.breaker = CircuitBreaker(settings)

    def runtime(self, db: Session) -> AgentRuntime:
        return AgentRuntime(db, self.settings, self.provider, self.embedder, self.registry, self.breaker)


_services: Services | None = None


def get_services() -> Services:
    global _services
    if _services is None:
        _services = Services(get_settings())
    return _services


def set_services(services: Services) -> None:
    global _services
    _services = services
