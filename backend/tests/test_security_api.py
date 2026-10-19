from tests.conftest import by_type


def test_login_failure_and_missing_token(client):
    assert client.post("/api/auth/login", json={"email": "admin@xinghe.demo", "password": "wrong"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_customer_cannot_use_staff_endpoints(client, login):
    customer = login("customer@xinghe.demo")
    assert client.get("/api/kb/documents", headers=customer).status_code == 403
    assert client.get("/api/approvals", headers=customer).status_code == 403
    assert client.get("/api/runs", headers=customer).status_code == 403


def test_agent_cannot_write_knowledge_base(client, login):
    r = client.post("/api/kb/documents/text", json={"title": "x", "content": "y"}, headers=login("agent@xinghe.demo"))
    assert r.status_code == 403


def test_tenants_are_isolated(client, ask, login):
    xinghe_admin = login("admin@xinghe.demo")
    qinghe_admin = login("admin@qinghe.demo")
    _, events = ask(login("customer@xinghe.demo"), "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    run_id = by_type(events, "done")[0]["run_id"]

    assert client.get(f"/api/approvals/{approval_id}", headers=qinghe_admin).status_code == 404
    assert client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"},
                       headers=qinghe_admin).status_code == 404
    assert client.get(f"/api/runs/{run_id}", headers=qinghe_admin).status_code == 404
    xinghe_docs = {d["id"] for d in client.get("/api/kb/documents", headers=xinghe_admin).json()}
    qinghe_docs = {d["id"] for d in client.get("/api/kb/documents", headers=qinghe_admin).json()}
    assert xinghe_docs.isdisjoint(qinghe_docs)
    some_doc = min(xinghe_docs)
    assert client.get(f"/api/kb/documents/{some_doc}", headers=qinghe_admin).status_code == 404

    _, qinghe_events = ask(login("customer@qinghe.demo"), "七天无理由退货有什么条件？")
    assert "没有找到" in by_type(qinghe_events, "final")[0]["text"]


def test_customer_cannot_read_other_customers_conversation(client, ask, login):
    conversation_id, _ = ask(login("customer@xinghe.demo"), "积分可以怎么抵扣？")
    other = login("customer2@xinghe.demo")
    assert client.get(f"/api/conversations/{conversation_id}/messages", headers=other).status_code == 404


def test_rate_limit(settings, client, login):
    settings.rate_limit_per_minute = 2
    headers = login("customer@xinghe.demo")
    codes = [client.post("/api/chat", json={"content": "积分怎么抵扣"}, headers=headers).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_upload_validation(client, login):
    admin = login("admin@xinghe.demo")
    bad = client.post("/api/kb/documents/upload", files={"file": ("a.exe", b"MZ", "application/octet-stream")},
                      headers=admin)
    assert bad.status_code == 415
    ok = client.post("/api/kb/documents/upload", files={"file": ("售后补充.md", "## 以旧换新\n- 以旧换新仅限手机品类。".encode())},
                     data={"visibility": "public"}, headers=admin)
    assert ok.status_code == 200 and ok.json()["chunk_count"] == 1
