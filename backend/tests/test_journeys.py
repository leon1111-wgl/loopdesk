"""Whole conversations through the real API, graded by the state they leave (app/evals/journeys.py)."""

from __future__ import annotations

import pytest

from app.db import new_session
from app.evals.journeys import compare, load, run_journey


@pytest.mark.parametrize("journey", load(), ids=lambda j: j["key"])
def test_journey_ends_where_it_should(client, journey):
    result = run_journey(client, new_session, journey)
    assert result["passed"], result["wrong"]


def test_a_wrong_end_state_fails():
    actual = {"orders": {"SO1": "paid"}, "refunds": {"SO1": 10.0}, "approvals": {"pending": 1}, "tickets": 1, "handoff": True, "last_message": "好"}
    assert compare({"orders": {"SO1": "refunding"}}, actual)
    assert compare({"refunds": {}}, actual)
    assert compare({"approvals": {}}, actual)
    assert compare({"tickets": 0}, actual)
    assert compare({"handoff": False}, actual)
    assert compare({"last_message": "凭证不足"}, actual)
    assert not compare({"orders": {"SO1": "paid"}, "refunds": {"SO1": 10.0}, "approvals": {"pending": 1}, "tickets": 1, "handoff": True}, actual)
