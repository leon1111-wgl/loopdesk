from tests.conftest import by_type

CONFLICTING = {"title": "到账补充说明", "content": "## 到账\n- 微信支付、支付宝退款预计1-2个工作日到账。"}


def _audit(client, headers, **params):
    r = client.get("/api/audit", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_risk_limit_change_is_recorded_with_who_before_after_and_the_request(client, login):
    admin = login("admin@leon.demo")
    r = client.patch("/api/tenant", json={"auto_refund_limit": 5000, "ticket_sla_hours": 24}, headers=admin)
    before = client.get("/api/audit", headers=admin).json()
    entry = before[0]
    assert entry["action"] == "tenant.update" and entry["action_label"] == "修改租户配置"
    assert entry["actor"]["email"] == "admin@leon.demo"
    assert entry["detail"]["changes"]["auto_refund_limit"][1] == 5000
    assert entry["detail"]["changes"]["auto_refund_limit"][0] != 5000
    assert entry["request_id"] == r.headers["X-Request-ID"], "joins the access log and the trace"
    # a PATCH that changes nothing leaves no entry
    client.patch("/api/tenant", json={"auto_refund_limit": 5000}, headers=admin)
    assert len(client.get("/api/audit", headers=admin).json()) == len(before)


def test_knowledge_changes_are_recorded_with_the_changed_lines(client, login):
    admin = login("admin@leon.demo")
    doc = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={
        "title": "到账补充说明", "content": "## 到账\n- 微信支付、支付宝退款预计1-3个工作日到账。",
        "visibility": "public", "version": doc["version"]})
    client.delete(f"/api/kb/documents/{doc['id']}", headers=admin)
    entries = _audit(client, admin)
    assert [e["action"] for e in entries[:3]] == ["kb.document.delete", "kb.document.update", "kb.document.create"]
    assert {e["target"] for e in entries[:3]} == {f"document:{doc['id']}"}
    diff = entries[1]["detail"]["diff"]
    assert "-- 微信支付、支付宝退款预计1-2个工作日到账。" in diff and "+- 微信支付、支付宝退款预计1-3个工作日到账。" in diff
    assert entries[1]["detail"]["from_version"] == 1 and entries[1]["detail"]["to_version"] == 2


def test_flywheel_conflict_and_regression_gate_decisions_are_recorded(client, ask, login):
    admin = login("admin@leon.demo")
    _, events = ask(login("customer@leon.demo"), "以旧换新活动怎么参加？")
    message_id = by_type(events, "final")[0]["message_id"]
    feedback = client.post(f"/api/messages/{message_id}/feedback", json={
        "rating": "correction", "reason_category": "knowledge_gap",
        "correction": "以旧换新活动仅限手机和平板品类，审核通过后可抵扣50-800元。"}, headers=login("agent@leon.demo")).json()
    client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", headers=admin)
    client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin)
    conflict_id = client.get("/api/kb/conflicts", headers=admin).json()[0]["id"]
    client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={"note": "以政策原文为准"}, headers=admin)
    case = client.get("/api/evals/cases", headers=admin).json()[0]
    client.patch(f"/api/evals/cases/{case['id']}", json={"status": "archived"}, headers=admin)
    actions = [e["action"] for e in _audit(client, admin)]
    for action in ("flywheel.patch.apply", "kb.conflict.resolve", "eval.case.update"):
        assert action in actions
    archived = _audit(client, admin, action="eval.case.update")[0]
    assert archived["detail"]["changes"]["status"] == [case["status"], "archived"]


def test_an_erasure_entry_does_not_keep_the_erased_identity(client, login):
    admin = login("admin@leon.demo")
    me = client.get("/api/auth/me", headers=login("customer@leon.demo")).json()
    client.delete(f"/api/admin/users/{me['id']}/personal-data", headers=admin)
    entry = _audit(client, admin, action="user.erase")[0]
    assert entry["target"] == f"user:{me['id']}"
    assert me["email"] not in str(entry) and me["name"] not in str(entry)


def test_the_audit_log_is_for_administrators_of_the_same_tenant(client, login):
    admin = login("admin@leon.demo")
    client.patch("/api/tenant", json={"auto_refund_limit": 300}, headers=admin)
    assert client.get("/api/audit", headers=login("agent@leon.demo")).status_code == 403
    assert client.get("/api/audit", headers=login("admin@iris.demo")).json() == []
