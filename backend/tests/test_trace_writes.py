"""Buffered trace writes (the PostgreSQL default) must record exactly what per-span writes do."""

from contextlib import contextmanager

import pytest
from sqlalchemy import event, select

from app.db import get_engine, new_session
from app.models import Run, Span
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
