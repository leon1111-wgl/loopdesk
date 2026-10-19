import logging

import pytest

from app.config import Settings
from app.main import create_app
from app.seed import DEMO_PASSWORD
from tests.conftest import _reset_database, by_type


def test_login_failure_and_missing_token(client):
    assert client.post("/api/auth/login", json={"email": "admin@leon.demo", "password": "wrong"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_customer_cannot_use_staff_endpoints(client, login):
    customer = login("customer@leon.demo")
    assert client.get("/api/kb/documents", headers=customer).status_code == 403
    assert client.get("/api/approvals", headers=customer).status_code == 403
    assert client.get("/api/runs", headers=customer).status_code == 403


def test_agent_cannot_write_knowledge_base(client, login):
    r = client.post("/api/kb/documents/text", json={"title": "x", "content": "y"}, headers=login("agent@leon.demo"))
    assert r.status_code == 403


def test_tenants_are_isolated(client, ask, login):
    leon_admin = login("admin@leon.demo")
    iris_admin = login("admin@iris.demo")
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    run_id = by_type(events, "done")[0]["run_id"]

    assert client.get(f"/api/approvals/{approval_id}", headers=iris_admin).status_code == 404
    assert client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                       headers=iris_admin).status_code == 404
    assert client.get(f"/api/runs/{run_id}", headers=iris_admin).status_code == 404
    leon_docs = {d["id"] for d in client.get("/api/kb/documents", headers=leon_admin).json()}
    iris_docs = {d["id"] for d in client.get("/api/kb/documents", headers=iris_admin).json()}
    assert leon_docs.isdisjoint(iris_docs)
    some_doc = min(leon_docs)
    assert client.get(f"/api/kb/documents/{some_doc}", headers=iris_admin).status_code == 404

    _, iris_events = ask(login("customer@iris.demo"), "七天无理由退货有什么条件？")
    assert "没有找到" in by_type(iris_events, "final")[0]["text"]


def test_customer_cannot_read_other_customers_conversation(client, ask, login):
    conversation_id, _ = ask(login("customer@leon.demo"), "积分可以怎么抵扣？")
    other = login("customer2@leon.demo")
    assert client.get(f"/api/conversations/{conversation_id}/messages", headers=other).status_code == 404


def test_rate_limit(settings, client, login):
    settings.rate_limit_per_minute = 2
    headers = login("customer@leon.demo")
    codes = [client.post("/api/chat", json={"content": "积分怎么抵扣"}, headers=headers).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_login_is_limited_per_client_across_accounts(settings, client):
    settings.login_attempts_per_ip = 5
    codes = [client.post("/api/auth/login", json={"email": f"user{i}@leon.demo", "password": "guess"}).status_code
             for i in range(6)]
    assert codes == [401] * 5 + [429], "a new email per attempt must not reset the budget"


def test_login_limit_per_account_ignores_case_and_whitespace(client):
    attempts = [" " * (i % 3) + ("Admin@Leon.Demo" if i % 2 else "admin@leon.demo") for i in range(11)]
    codes = [client.post("/api/auth/login", json={"email": e, "password": "guess"}).status_code for e in attempts]
    assert codes == [401] * 10 + [429]


def test_successful_logins_do_not_use_up_the_guessing_budget(client):
    good = [client.post("/api/auth/login", json={"email": "customer@leon.demo", "password": DEMO_PASSWORD}).status_code
            for _ in range(15)]
    assert good == [200] * 15
    bad = [client.post("/api/auth/login", json={"email": "customer@leon.demo", "password": "guess"}).status_code
           for _ in range(11)]
    assert bad == [401] * 10 + [429], "failures still count"


def test_rate_limiter_holds_under_a_parallel_burst():
    import threading

    from fastapi import HTTPException

    from app.security import RateLimiter

    limiter, allowed = RateLimiter(), []

    def attempt():
        try:
            limiter.check("login:burst", 10)
            allowed.append(1)
        except HTTPException:
            pass

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 10


@pytest.mark.parametrize("secret", [Settings().jwt_secret, "change-me-to-a-long-random-string-before-deploying",
                                    "short-secret"])
def test_production_config_refuses_a_public_or_short_jwt_secret(settings, secret):
    settings.seed_demo, settings.jwt_secret = False, secret
    with pytest.raises(RuntimeError, match="LOOPDESK_JWT_SECRET"):
        create_app(settings)


def test_demo_config_starts_but_warns_about_the_default_jwt_secret(settings, caplog):
    _reset_database(settings.database_url)
    settings.jwt_secret = Settings().jwt_secret
    with caplog.at_level(logging.WARNING, logger="app.main"):
        create_app(settings)
    assert "LOOPDESK_JWT_SECRET" in caplog.text


def test_production_config_starts_with_a_strong_secret(settings):
    _reset_database(settings.database_url)
    settings.seed_demo = False  # the fixture's secret is 36 random-looking bytes
    assert create_app(settings).title == "LoopDesk API"


def test_upload_validation(client, login):
    admin = login("admin@leon.demo")
    bad = client.post("/api/kb/documents/upload", files={"file": ("a.exe", b"MZ", "application/octet-stream")},
                      headers=admin)
    assert bad.status_code == 415
    ok = client.post("/api/kb/documents/upload", files={"file": ("售后补充.md", "## 以旧换新\n- 以旧换新仅限手机品类。".encode())},
                     data={"visibility": "public"}, headers=admin)
    assert ok.status_code == 200 and ok.json()["chunk_count"] == 1


def test_formatted_phone_numbers_never_reach_storage_in_plain_text(ask, login):
    from sqlalchemy import select

    from app.db import new_session
    from app.models import Message, Run, Span, Ticket

    ask(login("customer@leon.demo"), "扫地机器人坏了，开不了机，联系电话 +86 138-1234-5678，尽快处理")
    with new_session() as db:
        stored = [m.content for m in db.execute(select(Message)).scalars()]
        stored += [r.input_text for r in db.execute(select(Run)).scalars()]
        stored += [t.description for t in db.execute(select(Ticket)).scalars()]
        stored += [str(s.input) + str(s.output) for s in db.execute(select(Span)).scalars()]
    assert any("138****5678" in s for s in stored), "the masked form is what gets stored"
    assert not any("1234-5678" in s or "12345678" in s for s in stored)


# ---------------------------------------------------------------- permission matrix
# Reviewed on purpose. Any route not listed here must demand a login, and anything a
# customer may call is listed below; a new endpoint cannot become public by accident.
PUBLIC_ROUTES = {
    ("GET", "/api/health"), ("GET", "/api/ready"),
    ("GET", "/metrics"),  # aggregate counters only, no tenant data; restrict at the network layer
    ("POST", "/api/auth/login"), ("GET", "/api/auth/demo-accounts"),
    ("POST", "/api/channels/feishu/{tenant_slug}/events"),  # authenticated by Feishu's verification token
}
CUSTOMER_ROUTES = {
    ("GET", "/api/auth/me"), ("GET", "/api/conversations"), ("POST", "/api/conversations"),
    ("GET", "/api/conversations/{conversation_id}/messages"),
    ("POST", "/api/conversations/{conversation_id}/stream"), ("POST", "/api/chat"),
    ("POST", "/api/messages/{message_id}/feedback"),
}


def test_every_route_is_public_on_purpose_or_requires_the_right_role(client, login):
    import re

    customer = login("customer@leon.demo")
    seen = set()
    for path, operations in client.app.openapi()["paths"].items():
        for method in operations:
            route = (method.upper(), path)
            seen.add(route)
            url = re.sub(r"\{[^}]+\}", "1", path)
            if route not in PUBLIC_ROUTES:
                assert client.request(route[0], url, json={}).status_code == 401, f"{route} answers without a login"
            if route not in PUBLIC_ROUTES | CUSTOMER_ROUTES:
                code = client.request(route[0], url, json={}, headers=customer).status_code
                assert code == 403, f"{route} answers a customer with {code}"
    assert PUBLIC_ROUTES | CUSTOMER_ROUTES <= seen, "the lists name routes that no longer exist"


def test_feishu_webhook_fails_closed_outside_the_demo(settings, client):
    settings.seed_demo, settings.feishu_verification_token = False, None
    r = client.post("/api/channels/feishu/leon/events", json={"type": "url_verification", "challenge": "x"})
    assert r.status_code == 503
