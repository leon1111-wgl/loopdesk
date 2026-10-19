"""Provider-neutral message and tool-call types.

The agent runtime only speaks these types; each provider adapter translates them to its
wire format. Adding a vendor means writing one adapter, not touching the agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMMessage:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False
    # Provider-native assistant content (e.g. Claude content blocks incl. thinking blocks)
    # replayed unchanged on the next request.
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMMessage":
        calls = [ToolCall(**c) for c in data.get("tool_calls") or []]
        return cls(
            role=data["role"],
            content=data.get("content") or "",
            tool_calls=calls,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            is_error=bool(data.get("is_error")),
            raw=data.get("raw"),
        )


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    finish_reason: str = "stop"  # stop | tool_calls | length | refusal
    raw: Any = None


@dataclass
class StreamEvent:
    type: str  # delta | done
    text: str = ""
    result: ChatResult | None = None


class LLMError(Exception):
    """Non-retryable provider failure (bad request, auth, unknown model)."""


class LLMRetryableError(LLMError):
    """Rate limit, timeout, 5xx, connection reset."""


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        model: str,
        options: dict[str, Any] | None = None,
    ) -> ChatResult: ...

    def stream(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSpec],
        model: str,
        options: dict[str, Any] | None = None,
    ) -> Iterator[StreamEvent]:
        """Default: no token streaming, emit the whole completion at once."""
        result = self.chat(messages, tools, model, options)
        if result.content:
            yield StreamEvent(type="delta", text=result.content)
        yield StreamEvent(type="done", result=result)


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...
