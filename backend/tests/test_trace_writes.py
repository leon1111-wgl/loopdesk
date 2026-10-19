"""Buffered trace writes (the PostgreSQL default) must record exactly what per-span writes do."""

from contextlib import contextmanager

import pytest
from sqlalchemy import event, select

from app.db import get_engine, new_session
from app.models import AnswerCache, Approval, Order, Refund, Run, Span, Ticket
from app.services import get_services
from tests.conftest import by_type


@contextmanager
def _count_commits():
    counter = {"commits": 0}

    def on_commit(conn):
        counter["commits"] += 1

    event.listen(get_engine(), "commit", on_commit)
    try:
        yield counter
    finally:
        event.remove(get_engine(), "commit", on_commit)


def _spans(run_id):
    with new_session() as db:
        return [(s.kind, s.name) for s in db.execute(select(Span).where(Span.run_id == run_id).order_by(Span.id)).scalars()]


@pytest.mark.parametrize("question", ["退款多久能到账？", "SO20261008002 的快递到哪了？"])
def test_buffered_writes_record_the_same_spans_with_fewer_commits(settings, ask, login, question):
    customer = login("customer@leon.demo")
    settings.answer_cache_ttl_hours = 0  # the second ask must run the agent again, not hit the cache
    settings.trace_writes = "per_span"
    with _count_commits() as per_span:
        _, events = ask(customer, question)
    expected = _spans(events[0]["run_id"])
    settings.trace_writes = "buffered"
    with _count_commits() as buffered:
        _, events = ask(customer, question)
    assert _spans(events[0]["run_id"]) == expected
    assert buffered["commits"] < per_span["commits"] - 3, (buffered, per_span)


def test_buffered_writes_keep_the_spans_when_a_run_pauses_for_approval(settings, client, ask, login):
    settings.trace_writes = "buffered"
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    assert by_type(events, "approval_required")
    names = [name for _, name in _spans(events[0]["run_id"])]
    assert "request_refund" in names and "wecom_approval" in names


def test_buffered_writes_keep_the_spans_up_to_a_crash(settings, ask, login, monkeypatch):
    settings.trace_writes = "buffered"

    def broken(*args, **kwargs):
        raise RuntimeError("connection reset by upstream")

    monkeypatch.setattr(get_services().provider, "stream", broken)
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    with new_session() as db:
        assert db.get(Run, events[0]["run_id"]).status == "failed"
    kinds = [kind for kind, _ in _spans(events[0]["run_id"])]
    assert "guardrail" in kinds and "router" in kinds, "the spans before the crash are what operators need"


SCENARIOS = [
    ("customer@leon.demo", "退款多久能到账？"),          # answered from the knowledge base
    ("customer@leon.demo", "退款多久能到账？"),          # the same FAQ again: served from the answer cache
    ("customer@leon.demo", "SO20261008002 的快递到哪了？"),
    ("customer@leon.demo", "SO20260918001 申请退款"),    # automatic refund
    ("customer@leon.demo", "SO20261008002 我要退款"),    # waits for approval
    ("customer2@leon.demo", "转人工"),
    ("customer@leon.demo", "以旧换新活动怎么参加？"),     # refused
]


def _outcomes(client, ask, login):
    results = []
    for email, question in SCENARIOS:
        _, events = ask(login(email), question)
        final = by_type(events, "final")[0]
        with new_session() as db:
            run = db.get(Run, events[0]["run_id"])
            results.append((question, final["status"], tuple(final["meta"]["tools"]), run.route_tier,
                            bool(by_type(events, "approval_required"))))
    return results


def test_both_write_modes_produce_the_same_business_outcomes(settings, client, ask, login):
    # A performance change must not change behaviour: same statuses, tools, cache hits and approvals.
    settings.trace_writes = "per_span"
    per_span = _outcomes(client, ask, login)
    with new_session() as db:  # undo the first pass's side effects so the second starts from the same state
        for model in (AnswerCache, Approval, Refund, Ticket):
            db.query(model).delete()
        db.get(Order, "SO20260918001").status = "delivered"
        db.get(Order, "SO20261008002").status = "shipped"
        db.commit()
    settings.trace_writes = "buffered"
    buffered = _outcomes(client, ask, login)
    assert buffered == per_span
    assert per_span[1][3] == "cache" and per_span[5][1] == "handoff", "the scenarios cover cache hits and handoffs"
