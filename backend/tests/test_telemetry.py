"""OpenTelemetry export of agent runs (app/telemetry.py), through an in-memory exporter."""

import time

import pytest

from tests.conftest import by_type


@pytest.fixture()
def exported(settings, client):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import telemetry

    exporter = InMemorySpanExporter()
    telemetry.use_processor(settings, SimpleSpanProcessor(exporter))
    yield exporter
    telemetry.use_processor(settings, None)


def _roots(exporter):
    return [s for s in exporter.get_finished_spans() if s.parent is None]


def test_an_ended_run_is_one_trace_with_genai_attributes(exported, ask, login):
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    [root] = _roots(exported)
    assert root.name == "invoke_agent loopdesk"
    assert root.attributes["loopdesk.run.id"] == events[0]["run_id"]
    assert root.attributes["loopdesk.run.status"] == "completed"
    children = [s for s in exported.get_finished_spans() if s.parent is not None]
    assert children and all(s.context.trace_id == root.context.trace_id
                            and s.parent.span_id == root.context.span_id for s in children)
    chat = [s for s in children if s.attributes.get("gen_ai.operation.name") == "chat"]
    assert chat and chat[0].name == f"chat {chat[0].attributes['gen_ai.request.model']}"
    assert chat[0].attributes["gen_ai.usage.input_tokens"] > 0
    tools = [s.attributes["gen_ai.tool.name"] for s in children if s.attributes.get("gen_ai.operation.name") == "execute_tool"]
    assert "search_knowledge" in tools
    starts = [s.start_time for s in children]
    assert starts == sorted(starts) and root.start_time <= starts[0]
    assert root.end_time >= max(s.end_time for s in children)


def test_no_question_or_answer_text_leaves_the_service(exported, ask, login):
    _, events = ask(login("customer@leon.demo"), "我的手机号13812345678，退款多久能到账？")
    answer = by_type(events, "final")[0]["text"]
    values = " ".join(str(v) for s in exported.get_finished_spans() for v in s.attributes.values())
    assert "退款多久" not in values and "13812345678" not in values and answer[:12] not in values


def test_a_run_paused_for_approval_is_exported_once_when_it_ends(exported, client, ask, login):
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    assert exported.get_finished_spans() == (), "nothing while it waits for a person"
    client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=login("admin@leon.demo"))
    [root] = _roots(exported)
    assert root.attributes["loopdesk.run.id"] == events[0]["run_id"]
    names = [s.name for s in exported.get_finished_spans() if s.parent is not None]
    assert "execute_tool get_order" in names, "spans from before the pause belong to the same trace"
    assert any(n.startswith("approval ") for n in names)


def test_nothing_is_exported_unless_configured(client, ask, login):
    from app import telemetry

    assert telemetry._tracer is None
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    assert by_type(events, "final")[0]["status"] == "completed"


def test_a_buffered_span_keeps_the_time_it_started_not_the_time_it_was_written(settings, client):
    # Buffered trace writes insert every span at the run's end; the column default stamped them
    # all with that moment, so a timeline (and the OpenTelemetry export) collapsed into one instant.
    from app.agent.tracing import Tracer
    from app.db import new_session
    from app.models import Run, Span, Tenant, User

    with new_session() as db:
        tenant = db.query(Tenant).first()
        user = db.query(User).filter(User.tenant_id == tenant.id).first()
        run = Run(tenant_id=tenant.id, user_id=user.id)
        db.add(run)
        db.commit()
        tracer = Tracer(db, run, settings, buffered=True)
        with tracer.span("tool", "first"):
            time.sleep(0.05)
        with tracer.span("tool", "second"):
            pass
        tracer.flush()
        db.commit()
        first, second = db.query(Span).filter(Span.run_id == run.id).order_by(Span.id).all()
        assert (second.started_at - first.started_at).total_seconds() >= 0.05
