import logging

import pytest

from app.config import Settings
from app.main import create_app
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
