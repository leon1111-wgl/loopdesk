"""Trace recorder: every LLM call, retrieval, tool call, guardrail and verification is a
span.

Two ways to write them. Per span (SQLite default): each span is committed as it finishes,
so the trace view updates live; SQLite has one writer, and short transactions keep its
tail latency down. Buffered (PostgreSQL default): spans and token usage stay in memory and
are written at the run's own commit points (finish, pause for approval, failure), which
removes about two thirds of a request's SQL round trips; the trace appears when the run
ends. LOOPDESK_TRACE_WRITES=auto|per_span|buffered."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

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
    def __init__(self, db: Session, run: Run, settings: Settings, buffered: bool | None = None):
        self.db = db
        self.run = run
        self.settings = settings
        if buffered is None:
            mode = settings.trace_writes
            buffered = mode == "buffered" or (mode == "auto" and db.get_bind().dialect.name == "postgresql")
        self._buffer: list[Span] | None = [] if buffered else None
        self._usage: list[tuple[str, int, int]] = []  # buffered token usage: (model, prompt, completion)

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
            row = Span(
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
            usage = (handle.model or self.run.model, handle.prompt_tokens, handle.completion_tokens)
            if self._buffer is not None:
                self._buffer.append(row)
                if handle.prompt_tokens or handle.completion_tokens:
                    self._usage.append(usage)
            else:
                self.db.add(row)
                if handle.prompt_tokens or handle.completion_tokens:
                    self.add_usage(*usage)
                self.db.commit()

    def flush(self) -> None:
        """Buffered mode: hand the spans and token usage collected so far to the session; the
        caller's commit writes them. The buffer lives outside the session, so a rollback in
        between (a failing tool) cannot lose it. Per-span mode has nothing to flush."""
        if self._buffer:
            self.db.add_all(self._buffer)
            self._buffer.clear()
        for usage in self._usage:
            self.add_usage(*usage)
        self._usage.clear()

    def add_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        price_in, price_out = self.settings.price_for(model)
        self.run.prompt_tokens += prompt_tokens
        self.run.completion_tokens += completion_tokens
        self.run.cost = round(self.run.cost + (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)
