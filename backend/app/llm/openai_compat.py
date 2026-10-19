"""OpenAI-compatible chat adapter: Doubao (Ark), Hunyuan, DeepSeek, Qwen, OpenAI.

Streams with Server-Sent Events and reassembles tool-call fragments: providers send
``function.arguments`` in pieces keyed by ``index``, so the adapter concatenates them
per index and parses JSON only when the stream ends.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ..textutil import estimate_tokens
from .base import (
    ChatResult,
    Embedder,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMRetryableError,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def to_openai_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                        }
                        for c in m.tool_calls
                    ],
                }
            )
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools
    ]


def _parse_arguments(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"__invalid_json__": raw}
    return value if isinstance(value, dict) else {"value": value}


class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0, stream_usage: bool = True,
                 max_retries: int = 2, transport: httpx.BaseTransport | None = None):
        if not base_url or not api_key:
            raise LLMError("openai_compat provider needs LOOPDESK_LLM_BASE_URL (or LLM_PRESET) and LOOPDESK_LLM_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.stream_usage = stream_usage
        self.max_retries = max_retries
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def _payload(self, messages, tools, model, options, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(messages),
            "temperature": (options or {}).get("temperature", 0.2),
            "stream": stream,
        }
        if tools:
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = "auto"
        if stream and self.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                request = self.client.build_request("POST", f"{self.base_url}/chat/completions", json=payload)
                response = self.client.send(request, stream=bool(payload.get("stream")))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_retries:
                    raise LLMRetryableError(f"network error: {exc}") from exc
            else:
                if response.status_code < 400:
                    return response
                body = response.read().decode("utf-8", "replace")[:500]
                response.close()
                if response.status_code not in RETRYABLE_STATUS:
                    raise LLMError(f"HTTP {response.status_code}: {body}")
                if attempt == self.max_retries:
                    raise LLMRetryableError(f"HTTP {response.status_code}: {body}")
            time.sleep(delay)
            delay *= 2
        raise LLMRetryableError("unreachable")

    def chat(self, messages, tools, model, options=None) -> ChatResult:
        final: ChatResult | None = None
        for event in self.stream(messages, tools, model, options):
            if event.type == "done":
                final = event.result
        assert final is not None
        return final

    def stream(self, messages, tools, model, options=None) -> Iterator[StreamEvent]:
        payload = self._payload(messages, tools, model, options, stream=True)
        response = self._post_with_retry(payload)
        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        finish_reason = "stop"
        try:
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        yield StreamEvent(type="delta", text=delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        except httpx.HTTPError as exc:
            raise LLMRetryableError(f"stream interrupted: {exc}") from exc
        finally:
            response.close()

        content = "".join(content_parts)
        tool_calls = [
            ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=_parse_arguments(slot["arguments"]))
            for i, slot in sorted(calls.items())
        ]
        if usage is None:
            prompt_text = json.dumps(payload["messages"], ensure_ascii=False)
            usage = Usage(estimate_tokens(prompt_text), estimate_tokens(content), estimated=True)
        result = ChatResult(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            model=model,
            finish_reason="tool_calls" if tool_calls else finish_reason,
        )
        yield StreamEvent(type="done", result=result)


class OpenAICompatEmbedder(Embedder):
    def __init__(self, base_url: str, api_key: str, model: str, dim: int, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        if not (base_url and api_key and model):
            raise LLMError("openai_compat embeddings need LOOPDESK_EMBEDDING_BASE_URL / _API_KEY / _MODEL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}, transport=transport)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 16):
            batch = texts[start : start + 16]
            response = self.client.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": batch})
            if response.status_code >= 400:
                raise LLMError(f"embedding HTTP {response.status_code}: {response.text[:300]}")
            data = sorted(response.json()["data"], key=lambda d: d["index"])
            vectors.extend(d["embedding"] for d in data)
        return vectors
