"""Trace recorder: every LLM call, retrieval, tool call, guardrail and verification is a
span. Spans are committed as they finish, so the trace view updates live while a run
is still streaming."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..guardrails.pii import mask_pii
from ..models import Run, Span


def _scrub(value: Any) -> Any:
    """Mask PII in anything that is written to the trace store."""
    if isinstance(value, str):
        return mask_pii(value).masked
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


class SpanHandle:
    def __init__(self) -> None:
        self.output: dict[str, Any] = {}
        self.status = "ok"
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model: str | None = None

    def set(self, **output: Any) -> None:
        self.output.update(output)

    def fail(self, message: str) -> None:
        self.status = "error"
        self.output["error"] = message


class Tracer:
    def __init__(self, db: Session, run: Run, settings: Settings):
        self.db = db
        self.run = run
        self.settings = settings
        # PostgreSQL: span commits do not wait for the WAL flush. They carry trace rows and run
        # metadata only; business writes (tools, approvals, the final answer) commit on their
        # own, synchronously, and each synchronous commit makes the earlier ones durable too.
        # A crash can only lose the last spans of a run that did not finish anyway.
        self._async_commit = settings.trace_async_commit and db.get_bind().dialect.name == "postgresql"

    @contextmanager
    def span(self, kind: str, name: str, input: dict[str, Any] | None = None):
        handle = SpanHandle()
        started = time.perf_counter()
        try:
            yield handle
        except Exception as exc:
            handle.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            latency = int((time.perf_counter() - started) * 1000)
            self.db.add(
                Span(
                    run_id=self.run.id,
                    tenant_id=self.run.tenant_id,
                    kind=kind,
                    name=name,
                    input=_scrub(input or {}),
                    output=_scrub(handle.output),
                    status=handle.status,
                    latency_ms=latency,
                    prompt_tokens=handle.prompt_tokens,
                    completion_tokens=handle.completion_tokens,
                )
            )
            if handle.prompt_tokens or handle.completion_tokens:
                self.add_usage(handle.model or self.run.model, handle.prompt_tokens, handle.completion_tokens)
            if self._async_commit:
                self.db.execute(text("SET LOCAL synchronous_commit TO OFF"))
            self.db.commit()

    def add_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        price_in, price_out = self.settings.price_for(model)
        self.run.prompt_tokens += prompt_tokens
        self.run.completion_tokens += completion_tokens
        self.run.cost = round(self.run.cost + (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)
