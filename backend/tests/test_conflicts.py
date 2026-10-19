from tests.conftest import by_type

CONFLICTING = {"title": "到账补充说明", "content": "## 到账\n- 微信支付、支付宝退款预计1-2个工作日到账。"}


def test_seed_knowledge_base_has_no_conflicts(client, login):
    assert client.get("/api/kb/conflicts", headers=login("admin@leon.demo")).json() == []


def test_conflicting_document_is_flagged_on_both_sides(client, login):
    admin = login("admin@leon.demo")
    created = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    assert created["conflicts"] == 1
    conflicts = client.get("/api/kb/conflicts", headers=admin).json()
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["other_document"]["title"] == "售后与退款政策"
    assert c["detail"] == {"个工作日": {"this": ["1-2"], "other": ["1-3"]}}
    docs = {d["title"]: d["conflicts"] for d in client.get("/api/kb/documents", headers=admin).json()}
    assert docs["到账补充说明"] == 1 and docs["售后与退款政策"] == 1


def test_similar_wording_about_a_different_subject_is_not_a_conflict(client, login):
    admin = login("admin@leon.demo")
    for content in ("## 海外\n- 海外订单退款预计10个工作日到账。",
                    "## 银行卡\n- 银行卡支付的退款预计3-7个工作日到账，具体以银行处理时间为准。"):
        assert client.post("/api/kb/documents/text", json={"title": "x", "content": content},
                           headers=admin).json()["conflicts"] == 0


def test_resolve_and_permissions(client, login):
    admin = login("admin@leon.demo")
    client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin)
    conflict_id = client.get("/api/kb/conflicts", headers=admin).json()[0]["id"]
    assert client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={}, headers=login("agent@leon.demo")).status_code == 403
    assert client.get("/api/kb/conflicts", headers=login("admin@iris.demo")).json() == []
    assert client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={}, headers=login("admin@iris.demo")).status_code == 404
    r = client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={"note": "以政策原文为准"}, headers=admin)
    assert r.json()["status"] == "resolved"
    assert client.get("/api/kb/conflicts", headers=admin).json() == []
    docs = {d["title"]: d["conflicts"] for d in client.get("/api/kb/documents", headers=admin).json()}
    assert docs["售后与退款政策"] == 0


def test_deleting_a_document_clears_its_conflicts(client, login):
    admin = login("admin@leon.demo")
    doc_id = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()["id"]
    client.delete(f"/api/kb/documents/{doc_id}", headers=admin)
    assert client.get("/api/kb/conflicts?status_filter=all", headers=admin).json() == []


def test_flywheel_patch_that_contradicts_policy_reports_the_conflict(client, ask, login):
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    message_id = by_type(events, "final")[0]["message_id"]
    feedback = client.post(f"/api/messages/{message_id}/feedback", json={
        "rating": "correction", "reason_category": "knowledge_outdated",
        "correction": "微信支付、支付宝退款预计1-2个工作日到账。",
    }, headers=login("agent@leon.demo")).json()
    applied = client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", headers=login("admin@leon.demo")).json()
    assert applied["conflicts"] == 1, "the admin is told the patch contradicts the existing policy"
