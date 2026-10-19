"""Claude adapter (official ``anthropic`` SDK, optional dependency).

Differences from the OpenAI shape that this adapter absorbs:
- the system prompt is a top-level field, not a message;
- tool results go back as ``tool_result`` blocks inside ONE user message per turn;
- the assistant's content blocks (including thinking blocks) are replayed unchanged,
  so they are stored on ``LLMMessage.raw``;
- sampling parameters are not sent (current models reject them); depth is set with
  ``output_config.effort`` instead.
"""

from __future__ import annotations

import json
from typing import Any

from .base import ChatResult, LLMError, LLMMessage, LLMProvider, LLMRetryableError, ToolCall, ToolSpec, Usage


def to_anthropic(messages: list[LLMMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        if m.role == "tool":
            pending_results.append(
                {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content, "is_error": m.is_error}
            )
            continue
        flush_results()
        if m.role == "assistant":
            if m.raw:
                content: Any = m.raw
            else:
                content = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                content.extend(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls
                )
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": "user", "content": m.content})
    flush_results()
    return "\n\n".join(system_parts), out


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None, timeout: float = 120.0, effort: str = "low", client: Any = None):
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise LLMError("pip install anthropic to use LOOPDESK_LLM_PROVIDER=anthropic") from exc
            client = anthropic.Anthropic(api_key=api_key, timeout=timeout) if api_key else anthropic.Anthropic(timeout=timeout)
        self.client = client
        self.effort = effort

    def chat(self, messages, tools, model, options=None) -> ChatResult:
        system, wire_messages = to_anthropic(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 16000,
            "messages": wire_messages,
            "output_config": {"effort": self.effort},
            # On a safety decline the API re-runs the request on Anthropic's
            # recommended fallback model instead of returning the refusal.
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools)
        try:
            response = self.client.beta.messages.create(**kwargs)
        except Exception as exc:  # map SDK errors onto retryable / fatal
            raise _map_error(exc) from exc

        data = response.to_dict() if hasattr(response, "to_dict") else response
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [
            ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        usage = data.get("usage") or {}
        stop_reason = data.get("stop_reason")
        finish = {"tool_use": "tool_calls", "max_tokens": "length", "refusal": "refusal"}.get(stop_reason, "stop")
        if finish == "refusal":
            text, calls = "", []
        return ChatResult(
            content=text,
            tool_calls=calls,
            usage=Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
            model=data.get("model", model),
            finish_reason=finish,
            raw=json.loads(json.dumps(blocks, ensure_ascii=False, default=str)),
        )


def _map_error(exc: Exception) -> LLMError:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return LLMError(str(exc))
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return LLMRetryableError(str(exc))
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return LLMRetryableError(str(exc))
    return LLMError(str(exc))
