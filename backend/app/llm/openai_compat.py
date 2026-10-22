"""OpenAI-compatible chat adapter: Doubao (Ark), Hunyuan, DeepSeek, Qwen, OpenAI.

Streams with Server-Sent Events and reassembles tool-call fragments: providers send
``function.arguments`` in pieces keyed by ``index``, so the adapter concatenates them
per index and parses JSON only when the stream ends.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ..textutil import estimate_tokens
from .base import (
    ChatResult,
    Embedder,
    LLMDeadlineError,
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
                 max_retries: int = 2, transport: httpx.BaseTransport | None = None, stall_timeout: float = 0.0):
        if not base_url or not api_key:
            raise LLMError("openai_compat provider needs LOOPDESK_LLM_BASE_URL (or LLM_PRESET) and LOOPDESK_LLM_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.stream_usage = stream_usage
        self.max_retries = max_retries
        self.timeout = timeout
        self.stall_timeout = stall_timeout if 0 < stall_timeout < timeout else timeout
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0, read=self.stall_timeout),
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

    def _post_with_retry(self, payload: dict[str, Any], deadline: float | None = None) -> httpx.Response:
        """Retries transient failures, but never past the run's deadline (a monotonic time):
        each attempt's timeout is capped to the time left, and no retry starts after it.

        A model that sends nothing for ``stall_timeout`` is not retried (N-077): a hanging service
        would hang again, and three attempts would spend the whole run deadline on one tier. The
        runtime moves on to the other tier, then to the quoted-policy answer."""
        delay = 1.0
        fair = deadline is None or deadline - time.monotonic() >= self.stall_timeout
        for attempt in range(self.max_retries + 1):
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                if attempt == 0:  # never asked: says nothing about the model (N-108)
                    raise LLMDeadlineError("run deadline reached", blameless=True)
                raise LLMRetryableError("run deadline reached")
            try:
                timeout = None if left is None else httpx.Timeout(min(self.timeout, left), connect=min(10.0, left),
                                                                  read=min(self.stall_timeout, left))
                request = self.client.build_request("POST", f"{self.base_url}/chat/completions", json=payload,
                                                    **({"timeout": timeout} if timeout else {}))
                response = self.client.send(request, stream=bool(payload.get("stream")))
            except httpx.ReadTimeout as exc:
                raise self._stalled(deadline, fair) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_retries:
                    raise LLMRetryableError(f"network error: {exc}") from exc
            else:
                if response.status_code < 400:
                    return response
                try:  # an error status whose body then hangs is still that error, not a crash
                    body = response.read().decode("utf-8", "replace")[:500]
                except httpx.HTTPError:
                    body = "(body not received)"
                finally:
                    response.close()
                if response.status_code not in RETRYABLE_STATUS:
                    raise LLMError(f"HTTP {response.status_code}: {body}")
                if attempt == self.max_retries:
                    raise LLMRetryableError(f"HTTP {response.status_code}: {body}")
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise LLMRetryableError("run deadline reached")
            time.sleep(delay)
            delay *= 2
        raise LLMRetryableError("unreachable")

    def _stalled(self, deadline: float | None, fair: bool) -> LLMRetryableError:
        if deadline is not None and time.monotonic() >= deadline - 0.05:  # cut short by the run's deadline
            # Too little of the run was left to judge the model: the breaker does not count it.
            return LLMDeadlineError("run deadline reached while waiting for the model", blameless=not fair)
        return LLMRetryableError(f"stall timeout: the model sent nothing for {self.stall_timeout:g} s")

    def _cut_off(self, progress_at: float, call_deadline: float, deadline: float | None,
                 had_fair_chance: bool) -> LLMRetryableError:
        now = time.monotonic()
        past_deadline = deadline is not None and now >= deadline - 0.05
        if past_deadline and not had_fair_chance:  # too little of the run was left to judge the model
            return LLMDeadlineError("run deadline reached while the model was answering", blameless=True)
        if now - progress_at >= self.stall_timeout - 0.05:
            return LLMRetryableError(f"stall timeout: the model sent no output for {self.stall_timeout:g} s")
        if past_deadline:
            return LLMDeadlineError("run deadline reached while the model was answering")
        return LLMRetryableError(f"call timeout: the model was still answering after {self.timeout:g} s")

    def chat(self, messages, tools, model, options=None) -> ChatResult:
        final: ChatResult | None = None
        for event in self.stream(messages, tools, model, options):
            if event.type == "done":
                final = event.result
        assert final is not None
        return final

    def stream(self, messages, tools, model, options=None) -> Iterator[StreamEvent]:
        payload = self._payload(messages, tools, model, options, stream=True)
        deadline = (options or {}).get("deadline")
        started = time.monotonic()
        # The whole call, not each read, is bounded (N-108): httpx's read timeout resets on every
        # byte, so a model sending an empty delta every 8 s kept one answer going for 242 s.
        call_deadline = started + self.timeout if deadline is None else min(started + self.timeout, deadline)
        had_fair_chance = call_deadline - started >= self.stall_timeout
        response = self._post_with_retry(payload, deadline=deadline)
        content_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        finish_reason = "stop"
        lines = _LineReader(response)
        progress_at = time.monotonic()  # when output last arrived; heartbeats and empty deltas do not move it
        try:
            while True:
                now = time.monotonic()
                wait = min(progress_at + self.stall_timeout, call_deadline) - now
                if now >= call_deadline:  # a long backlog does not outlast the deadline
                    raise self._cut_off(progress_at, call_deadline, deadline, had_fair_chance)
                arrived, line = lines.next(wait)
                if line is _TIMED_OUT:
                    raise self._cut_off(progress_at, call_deadline, deadline, had_fair_chance)
                if line is _END:
                    break
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                    progress_at = arrived
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content") or delta.get("reasoning"):  # thinking models: output, not shown
                        progress_at = arrived
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        progress_at = arrived
                        yield StreamEvent(type="delta", text=delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["arguments"] += fn.get("arguments") or ""
                        progress_at = arrived
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                        progress_at = arrived
        except httpx.ReadTimeout as exc:
            raise self._stalled(deadline, had_fair_chance) from exc
        except httpx.HTTPError as exc:
            raise LLMRetryableError(f"stream interrupted: {exc}") from exc
        finally:
            lines.stop()
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


_END = object()
_TIMED_OUT = object()


class _LineReader:
    """Reads the response's lines on a daemon thread so the caller can stop waiting on its own
    clock. Once stopped (the caller gave up and closed the response), the thread ends at its next
    line or when the closed connection errors out."""

    def __init__(self, response: httpx.Response):
        self._queue: queue.Queue = queue.Queue()
        self._stopped = threading.Event()
        threading.Thread(target=self._read, args=(response,), daemon=True).start()

    def _read(self, response: httpx.Response) -> None:
        try:
            for line in response.iter_lines():
                if self._stopped.is_set():
                    return
                self._queue.put((time.monotonic(), line))
            self._queue.put((time.monotonic(), _END))
        except BaseException as exc:  # handed to the caller, raised there
            self._queue.put((time.monotonic(), exc))

    def next(self, timeout: float) -> tuple[float, Any]:
        """(arrival time, line). A line already received is returned even when the wait is used up:
        a consumer that paused (a slow client) must not turn a healthy stream into a stall."""
        try:
            arrived, item = self._queue.get_nowait() if timeout <= 0 else self._queue.get(timeout=timeout)
        except queue.Empty:
            return time.monotonic(), _TIMED_OUT
        if isinstance(item, BaseException):
            raise item
        return arrived, item

    def stop(self) -> None:
        self._stopped.set()


class OpenAICompatEmbedder(Embedder):
    def __init__(self, base_url: str, api_key: str, model: str, dim: int, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None, query_timeout: float | None = None):
        if not (base_url and api_key and model):
            raise LLMError("openai_compat embeddings need LOOPDESK_EMBEDDING_BASE_URL / _API_KEY / _MODEL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}, transport=transport)
        # A search waits this long for its query vector, then goes on with BM25 (N-078);
        # ingest and the backfill keep the longer timeout.
        self.query_timeout = query_timeout or timeout

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text], timeout=self.query_timeout)[0]

    def embed(self, texts: list[str], timeout: float | None = None) -> list[list[float]]:
        vectors: list[list[float]] = []
        extra = {"timeout": timeout} if timeout is not None else {}
        for start in range(0, len(texts), 16):
            batch = texts[start : start + 16]
            response = self.client.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": batch},
                                        **extra)
            if response.status_code >= 400:
                raise LLMError(f"embedding HTTP {response.status_code}: {response.text[:300]}")
            data = sorted(response.json()["data"], key=lambda d: d["index"])
            for item in data:
                vector = item["embedding"]
                if len(vector) != self.dim:  # the index and the query must agree, and the config says which
                    raise LLMError(f"embedding model {self.model} returned {len(vector)} dimensions, "
                                   f"LOOPDESK_EMBEDDING_DIM is {self.dim}")
                norm = math.sqrt(sum(v * v for v in vector)) or 1.0  # the index takes dot products as cosines
                vectors.append([v / norm for v in vector])
        return vectors
