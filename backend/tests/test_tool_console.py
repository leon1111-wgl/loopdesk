"""The tool console (10-29 ③): money tools within the tenant's refund tiers (N-228), tool errors a customer can
read (N-229), the list the console shows and the demo tenant's own tools (N-231)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import new_session
from app.main import create_app
from app.models import AuditLog, Span, Tenant, TenantTool
from app.seed import DEMO_PASSWORD
from tests.conftest import _reset_database, by_type
from tests.test_tenant_tools import VOUCHER, _create, _final, backoffice  # noqa: F401  (fixture)


def _voucher(**approval):
    return {**VOUCHER, "approval": {**VOUCHER["approval"], **approval}}


# ---------------------------------------------------------------- N-228
@pytest.mark.parametrize("approval, why", [
    ({"auto_limits": {"CNY": 100000}}, "自动退款额度"),
    ({"auto_limits": {"CNY": 60}}, "自动退款额度"),
    ({"admin_above": {"CNY": 900}}, "必须由管理员审批"),
    ({"auto_limits": {"CNY": 20, "USD": 5000}}, "只能按本租户币种"),
])
def test_a_money_tool_is_never_looser_than_the_refund_tiers(client, login, backoffice, approval, why):  # noqa: F811
    r = _create(client, login, _voucher(**approval))
    assert r.status_code == 422 and why in r.json()["detail"], r.text


def test_an_always_policy_without_an_admin_line_gets_the_tenants(client, login, backoffice):  # noqa: F811
    body = {**VOUCHER, "approval": {"mode": "always", "approver": "agent"}}
    r = _create(client, login, body)
    assert r.status_code == 201, r.text
    assert r.json()["approval"]["admin_above"] == {"CNY": 500.0}
    with new_session() as db:
        entry = db.execute(select(AuditLog).where(AuditLog.action == "tool.create")).scalar_one()
    assert entry.detail["approval"]["admin_above"] == {"CNY": 500.0}, "the audit says what the tool may do"
    assert entry.detail["url"].startswith("https://")


def test_a_looser_policy_stored_before_the_caps_is_held_to_them(client, login, ask, backoffice):  # noqa: F811
    tool = _create(client, login, VOUCHER).json()
    with new_session() as db:  # as a row written before this check existed
        row = db.get(TenantTool, tool["id"])
        row.approval = {**row.approval, "auto_limits": {"CNY": 100000}, "approver": "agent", "admin_above": {}}
        db.commit()
    _, events = ask(login("customer@leon.demo"), "快递丢了，给我发 800 元补偿券")
    approval = by_type(events, "approval_required")[0]
    assert backoffice.calls("POST", "/v1/vouchers") == [], "over the tenant's automatic limit: a person decides"
    item = client.get(f"/api/approvals/{approval['approval_id']}", headers=login("agent@leon.demo")).json()
    assert item["required_role"] == "admin", "over the tenant's administrator line"
    r = client.post(f"/api/approvals/{approval['approval_id']}/decide", json={"decision": "approve"},
                    headers=login("agent@leon.demo"))
    assert r.status_code == 403


# ---------------------------------------------------------------- N-229
def test_a_missing_secret_is_not_told_to_the_customer(client, login, ask, backoffice, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LOOPDESK_SECRET_LEON_POINTS")
    _create(client, login, VOUCHER)
    _, events = ask(login("customer@leon.demo"), "快递丢了，给我发 10 元补偿券")
    text = _final(events)
    assert "LEON_POINTS" not in text and "密钥" not in text and "暂时办理不了" in text, text
    with new_session() as db:  # nothing was sent, so no ledger row: the reason is in the trace for the operator
        span = db.execute(select(Span).where(Span.kind == "tool", Span.name == "issue_voucher")).scalar_one()
    assert "LEON_POINTS" in span.output["reason"] and "LEON_POINTS" not in str(span.output["output"])


def test_a_reviewer_reads_why_carrying_out_failed(client, login, ask, backoffice, monkeypatch):  # noqa: F811
    _create(client, login, VOUCHER)
    _, events = ask(login("customer@leon.demo"), "快递丢了，给我发 80 元补偿券")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    monkeypatch.delenv("LOOPDESK_SECRET_LEON_POINTS")
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=login("agent@leon.demo"))
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "NotSent" not in detail and "LEON_POINTS 未配置" in detail and "可以重新审批" in detail, detail


# ---------------------------------------------------------------- N-231
def test_the_console_lists_calls_errors_and_waiting_applications(client, login, ask, backoffice):  # noqa: F811
    _create(client, login, VOUCHER)
    emma = login("customer@leon.demo")
    ask(emma, "快递丢了，给我发 10 元补偿券")
    ask(emma, "快递丢了，给我发 80 元补偿券")
    ask(login("customer2@leon.demo"), "快递丢了，给我发 9 元补偿券")  # Jack's account is frozen: a refusal
    listing = client.get("/api/admin/tools", headers=login("agent@leon.demo")).json()
    tool = listing["tools"][0]
    assert tool["stats"]["calls"] == 3 and tool["stats"]["errors"] == 1 and tool["stats"]["last_error"], tool["stats"]
    assert tool["pending_approvals"] == 1
    assert listing["tiers"] == {"currency": "CNY", "auto_refund_limit": 50.0, "approval_admin_threshold": 500.0}
    builtin = {b["name"]: b for b in listing["builtin"]}
    assert builtin["search_knowledge"]["stats"]["calls"] == 0 and "pending_approvals" in builtin["request_refund"]


# ---------------------------------------------------------------- the demo tenant's own tools
@pytest.fixture()
def demo(settings):
    from app.rag.retrieval import index_manager

    settings.seed_demo_tools = True
    _reset_database(settings.database_url)
    index_manager._cache.clear()
    index_manager._tokens.clear()
    with TestClient(create_app(settings)) as c:
        yield c


def _login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _chat(client, headers, text):
    return client.post("/api/chat", json={"content": text}, headers=headers).json()


def test_the_demo_tenant_declares_two_tools_against_a_synthetic_back_office(demo):
    with new_session() as db:
        leon = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        names = sorted(db.execute(select(TenantTool.name).where(TenantTool.tenant_id == leon.id)).scalars())
    assert leon.tool_hosts == ["api.leon.example"] and names == ["compensation_voucher", "member_points"]
    emma, mia = _login(demo, "customer@leon.demo"), _login(demo, "agent@leon.demo")
    assert "1280 积分" in _chat(demo, emma, "我的积分还有多少？")["answer"]
    small = _chat(demo, emma, "快递晚了三天，给我发 5 元补偿券")
    assert small["approval"] is None and "已为您发放 5.00 元补偿券" in small["answer"]
    big = _chat(demo, emma, "快递丢了，给我发 80 元补偿券")
    assert big["status"] == "waiting_approval" and "超过 20 元的补偿券需要人工核实" in big["answer"]
    r = demo.post(f"/api/approvals/{big['approval']['approval_id']}/decide", json={"decision": "approve", "comment": "属实"},
                  headers=mia)
    assert r.status_code == 200 and "已为您发放 80.00 元补偿券" in r.json()["final"]["text"]
    # a staff question about the voucher rule is still the knowledge base's, not a call
    staff = _chat(demo, mia, "一线客服最多可以发放多少钱的补偿券？")
    assert staff["tools"] == ["search_knowledge"]
