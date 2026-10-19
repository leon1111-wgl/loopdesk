"""Optional OpenTelemetry export of agent runs (LOOPDESK_OTLP_ENDPOINT).

A run that has ended becomes one trace: a root span ``invoke_agent loopdesk`` and a child per
LoopDesk span, timed from the stored spans and named and tagged with the OpenTelemetry GenAI
semantic conventions (``chat <model>`` with token usage, ``execute_tool <tool>``), so
Jaeger, Grafana Tempo, Langfuse and similar backends show model calls, tokens and tool calls
without a custom adapter. A run paused for approval is exported once, when it finally ends,
including the spans recorded before the pause (they are read back from the trace store).

Only metadata leaves the service: no questions, answers, prompts or tool arguments. Those stay
in LoopDesk's own store, behind tenant isolation, PII masking and the retention policy.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Run, Span

logger = logging.getLogger(__name__)
_tracer: Any = None  # an opentelemetry Tracer once configured


def configure(settings: Settings) -> None:
    """Export to an OTLP/HTTP endpoint in batches, off the request path."""
    global _tracer
    if not settings.otlp_endpoint:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover - the exporter is in requirements.txt
        logger.warning("LOOPDESK_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp-proto-http is not installed")
        return
    _tracer = _provider(settings, BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint)))


def use_processor(settings: Settings, processor) -> None:
    """Tests: export through the given span processor (e.g. an in-memory exporter)."""
    global _tracer
    _tracer = _provider(settings, processor) if processor is not None else None


def _provider(settings: Settings, processor):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create({"service.name": settings.otlp_service_name}))
    provider.add_span_processor(processor)
    return provider.get_tracer("loopdesk")


def as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)  # SQLite returns naive values


def _ns(moment: datetime) -> int:
    return int(as_utc(moment).timestamp() * 1_000_000_000)


def _child(span: Span) -> tuple[str, dict[str, Any], Any]:
    from opentelemetry.trace import SpanKind

    attributes: dict[str, Any] = {"loopdesk.span.kind": span.kind, "loopdesk.span.status": span.status}
    if span.kind == "llm":
        model = span.name.split(":", 1)[-1]
        attributes.update({"gen_ai.operation.name": "chat", "gen_ai.request.model": model,
                           "gen_ai.usage.input_tokens": span.prompt_tokens,
                           "gen_ai.usage.output_tokens": span.completion_tokens})
        return f"chat {model}", attributes, SpanKind.CLIENT
    if span.kind == "tool":
        attributes.update({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": span.name})
        return f"execute_tool {span.name}", attributes, SpanKind.INTERNAL
    return f"{span.kind} {span.name}", attributes, SpanKind.INTERNAL


def export_run(db: Session, run: Run, settings: Settings) -> None:
    """Send an ended run as one trace. Never raises: telemetry must not fail a request."""
    if _tracer is None or run.is_eval:
        return
    try:
        _export(db, run, settings)
    except Exception:  # pragma: no cover - defensive
        logger.exception("exporting run %s to OpenTelemetry failed", run.id)


def _export(db: Session, run: Run, settings: Settings) -> None:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode

    spans = db.execute(select(Span).where(Span.run_id == run.id).order_by(Span.id)).scalars().all()
    ends = [as_utc(s.started_at) + timedelta(milliseconds=s.latency_ms) for s in spans]
    started = min([as_utc(run.created_at)] + [as_utc(s.started_at) for s in spans])
    finished = max([as_utc(run.finished_at or datetime.now(timezone.utc))] + ends)
    root = _tracer.start_span("invoke_agent loopdesk", kind=SpanKind.INTERNAL, start_time=_ns(started), attributes={
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "loopdesk",
        "gen_ai.provider.name": settings.llm_provider, "gen_ai.request.model": run.model or "",
        "gen_ai.usage.input_tokens": run.prompt_tokens, "gen_ai.usage.output_tokens": run.completion_tokens,
        "gen_ai.conversation.id": str(run.conversation_id or ""),
        "loopdesk.run.id": run.id, "loopdesk.tenant.id": run.tenant_id, "loopdesk.run.status": run.status,
        "loopdesk.run.steps": run.steps, "loopdesk.run.route_tier": run.route_tier or "",
        "loopdesk.run.cost": run.cost, "loopdesk.evidence.verdict": run.verdict or "",
        "loopdesk.evidence.coverage": run.evidence_coverage if run.evidence_coverage is not None else -1.0,
    })
    if run.status == "failed":
        root.set_status(Status(StatusCode.ERROR, (run.error or "").split(":", 1)[0]))  # the exception type only
    parent = trace.set_span_in_context(root)
    for span, end in zip(spans, ends):
        name, attributes, kind = _child(span)
        child = _tracer.start_span(name, context=parent, kind=kind, start_time=_ns(span.started_at),
                                   attributes=attributes)
        if span.status == "error":
            child.set_status(Status(StatusCode.ERROR))
        child.end(end_time=_ns(end))
    root.end(end_time=_ns(finished))
