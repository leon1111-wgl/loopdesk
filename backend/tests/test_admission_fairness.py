"""One tenant's flood fills its own share of the slots, not the process; one user's runs fill
only their own few (N-181).

``python -m app.evals.load_bench --model-latency 6 --fairness`` measures it against a real
server; the numbers are in docs/PERFORMANCE.md."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.admission import OVERLOADED, USER_BUSY, Admission, get_admission
from app.db import new_session
from app.models import User


def _turned_away(admission: Admission, tenant, user) -> tuple[int, str]:
    with pytest.raises(HTTPException) as caught:
        admission.enter(tenant, user)
    assert caught.value.headers == {"Retry-After": "5"}
    return caught.value.status_code, caught.value.detail


def test_a_tenant_fills_its_share_and_other_tenants_still_get_in():
    admission = Admission(limit=4, tenant_share=0.5, per_user=4)
    flood = [admission.enter("A", user) for user in (1, 2)]
    assert _turned_away(admission, "A", 3) == (503, OVERLOADED)  # A's share (2 of 4) is taken
    b = [admission.enter("B", user) for user in (7, 8)]  # the other half is still open for B
    assert _turned_away(admission, "C", 9) == (503, OVERLOADED)  # now the process is full
    assert admission.rejections == {("tenant", "A"): 1, ("process", "C"): 1}
    admission.leave(flood[0])
    assert admission.by_tenant == {"A": 1, "B": 2}
    admission.enter("A", 3)  # a finished run gives its tenant's slot back too
    for slot in [flood[1], *b]:
        admission.leave(slot)
    assert admission.inflight == 1 and admission.by_user == {3: 1}


def test_one_user_gets_429_for_their_own_runs_while_others_get_in():
    admission = Admission(limit=10, per_user=2)
    admission.enter("A", 1)
    admission.enter("A", 1)
    assert _turned_away(admission, "A", 1) == (429, USER_BUSY)
    admission.enter("A", 2)  # same tenant, another customer
    assert admission.rejections == {("user", "A"): 1}


def test_a_single_tenant_deployment_can_use_every_slot():
    admission = Admission(limit=3, tenant_share=1.0)
    for user in range(3):
        admission.enter("A", user)
    assert _turned_away(admission, "A", 9)[0] == 503
    assert admission.rejections == {("process", "A"): 1}
    # A tiny limit still lets each tenant run something.
    assert Admission(limit=1, tenant_share=0.75).tenant_limit == 1


def _ids(email: str) -> tuple[int, int]:
    with new_session() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        return user.tenant_id, user.id


@pytest.fixture()
def four_slots(settings):
    settings.max_inflight_runs = 4
    settings.tenant_inflight_share = 0.5
    settings.user_inflight_runs = 2
    return settings


def test_leon_flooding_leaves_iris_answered(four_slots, client, login):
    emma, noah = login("customer@leon.demo"), login("customer@iris.demo")
    leon, _ = _ids("customer@leon.demo")
    admission = get_admission()
    held = [admission.enter(leon, -1), admission.enter(leon, -2)]  # two Leon customers waiting on the model
    try:
        r = client.post("/api/chat", json={"content": "退款多久能到账？"}, headers=emma)
        assert r.status_code == 503 and r.json()["detail"] == OVERLOADED
        r = client.post("/api/chat", json={"content": "怎么开发票？"}, headers=noah)
        assert r.status_code == 200 and r.json()["answer"]
        iris, _ = _ids("customer@iris.demo")
        metrics = client.get("/metrics").text
        assert f'loopdesk_runs_rejected_total{{reason="tenant",tenant="{leon}"}} 1\n' in metrics
        assert f'loopdesk_inflight_runs_by_tenant{{tenant="{leon}"}} 2\n' in metrics
        assert f'tenant="{iris}"}} ' not in metrics.split("loopdesk_runs_rejected_total counter")[1]
    finally:
        for slot in held:
            admission.leave(slot)
    assert admission.inflight == 0 and admission.by_tenant == {}


def test_a_user_with_answers_still_running_gets_429_on_both_paths(four_slots, client, login):
    jack, emma = login("customer2@leon.demo"), login("customer@leon.demo")
    leon, jack_id = _ids("customer2@leon.demo")
    conversation_id = client.post("/api/conversations", json={}, headers=jack).json()["id"]
    admission = get_admission()
    held = [admission.enter(leon, jack_id), admission.enter(leon, jack_id)]
    try:
        r = client.post("/api/chat", json={"content": "退款多久能到账？"}, headers=jack)
        assert r.status_code == 429 and r.json()["detail"] == USER_BUSY and r.headers["retry-after"] == "5"
        r = client.post(f"/api/conversations/{conversation_id}/stream", json={"content": "退款多久能到账？"},
                        headers=jack)
        assert r.status_code == 429
    finally:
        for slot in held:
            admission.leave(slot)
    # Once those finish, both customers are answered again.
    assert client.post("/api/chat", json={"content": "积分怎么抵扣？"}, headers=emma).status_code == 200
    assert client.post("/api/chat", json={"content": "积分怎么抵扣？"}, headers=jack).status_code == 200
    assert admission.inflight == 0


def test_staff_and_integration_accounts_are_held_only_by_the_tenant_share(four_slots, client, login):
    # One staff or integration account answers for many people, so the per-user cap is for customers.
    mia = login("agent@leon.demo")
    _, mia_id = _ids("agent@leon.demo")
    admission = get_admission()
    held = [admission.enter(None, mia_id), admission.enter(None, mia_id)]  # at the per-user cap of 2
    try:
        assert client.post("/api/chat", json={"content": "退款多久能到账？"}, headers=mia).status_code == 200
    finally:
        for slot in held:
            admission.leave(slot)
