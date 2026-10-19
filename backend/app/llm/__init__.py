from __future__ import annotations

from ..config import Settings
from .base import LLMError, LLMProvider


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        from .mock import MockLLM

        return MockLLM(stream_delay=settings.mock_stream_delay)
    if settings.llm_provider == "openai_compat":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            settings.llm_base_url or "", settings.llm_api_key or "",
            timeout=settings.llm_timeout, stream_usage=settings.llm_stream_usage,
        )
    if settings.llm_provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings.llm_api_key, timeout=settings.llm_timeout, effort=settings.anthropic_effort)
    raise LLMError(f"unknown LOOPDESK_LLM_PROVIDER: {settings.llm_provider}")
