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
    assert c["detail"] == {"时长": {"this": ["1-2个工作日"], "other": ["1-3个工作日"]}}
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


def test_flywheel_patch_that_contradicts_policy_needs_an_acknowledgement(client, ask, login):
    _, events = ask(login("customer@leon.demo"), "退款多久能到账？")
    message_id = by_type(events, "final")[0]["message_id"]
    feedback = client.post(f"/api/messages/{message_id}/feedback", json={
        "rating": "correction", "reason_category": "knowledge_outdated",
        "correction": "微信支付、支付宝退款预计1-2个工作日到账。",
    }, headers=login("agent@leon.demo")).json()
    admin = login("admin@leon.demo")
    blocked = client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", headers=admin)
    assert blocked.status_code == 409
    conflicts = blocked.json()["conflicts"]
    assert [c["existing_sentence"] for c in conflicts] == ["微信支付、支付宝退款预计1-3个工作日到账。"], \
        "compared with the line it is about, not with the bank card line"
    applied = client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", json={"acknowledge_conflicts": True},
                          headers=admin).json()
    assert applied["conflicts"] == 1 and applied["eval_case_activated"] is False


def test_a_correction_in_other_units_is_caught_and_cannot_certify_itself(client, ask, login):
    # Review round 2, N-014: "3天" was not compared with "个工作日", the patch was published next to
    # the policy, and its eval case went active with must_include "3天".
    _, events = ask(login("customer@leon.demo"), "退款多久能到账啊")
    message_id = by_type(events, "final")[0]["message_id"]
    feedback = client.post(f"/api/messages/{message_id}/feedback", json={
        "rating": "correction", "reason_category": "knowledge_outdated", "correction": "退款审核通过后3天内原路到账。",
    }, headers=login("agent@leon.demo")).json()
    admin = login("admin@leon.demo")
    blocked = client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", headers=admin)
    assert blocked.status_code == 409
    assert {c["detail"]["时长"]["other"][0] for c in blocked.json()["conflicts"]} == {"1-3个工作日", "3-7个工作日"}
    client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", json={"acknowledge_conflicts": True}, headers=admin)
    case = next(c for c in client.get("/api/evals/cases", headers=admin).json() if c["id"] == feedback["eval_case_id"])
    assert case["status"] == "candidate", "a disputed correction must not lock itself into the regression gate"


def test_a_question_resting_on_both_sides_of_an_open_conflict_goes_to_a_person(client, ask, login):
    # N-014, the remaining layer: until someone checks which version is right, the agent does not
    # answer from contradicting knowledge (the evidence check passes each sentence on its own).
    admin = login("admin@leon.demo")
    client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin)
    _, events = ask(login("customer@leon.demo"), "微信退款多久能到账？")
    final = by_type(events, "final")[0]
    assert final["status"] == "handoff" and final["text"].startswith("抱歉，这个问题涉及的规定正在核对中")
    assert "1-2个工作日" not in final["text"] and final["meta"]["citations"] == [], \
        "the customer is not shown the two versions"
    trace = client.get(f"/api/runs/{events[0]['run_id']}", headers=admin).json()
    assert any(s["name"] == "knowledge_conflict" for s in trace["spans"])

    _, events = ask(login("agent@leon.demo"), "微信退款多久能到账？")
    staff = by_type(events, "final")[0]
    assert "1-2个工作日" in staff["text"] and "1-3个工作日" in staff["text"], "staff see what to check"

    conflict_id = client.get("/api/kb/conflicts", headers=admin).json()[0]["id"]
    client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={"note": "补充说明为准"}, headers=admin)
    _, events = ask(login("customer@leon.demo"), "微信退款多久能到账？")
    assert by_type(events, "final")[0]["status"] == "completed", "answered again once resolved"


def test_a_correction_published_anyway_stays_on_the_list_until_the_policy_is_edited(client, ask, login):
    from sqlalchemy import select

    from app.db import new_session
    from app.models import Document
    from app.rag.ingest import ingest_document
    from app.services import get_services

    _, events = ask(login("customer@leon.demo"), "退款多久能到账啊")
    message_id = by_type(events, "final")[0]["message_id"]
    feedback = client.post(f"/api/messages/{message_id}/feedback", json={
        "rating": "correction", "reason_category": "knowledge_outdated", "correction": "退款审核通过后3天内原路到账。",
    }, headers=login("agent@leon.demo")).json()
    admin = login("admin@leon.demo")
    client.post(f"/api/flywheel/patches/{feedback['patch_id']}/apply", json={"acknowledge_conflicts": True}, headers=admin)
    listed = client.get("/api/kb/conflicts", headers=admin).json()
    assert {c["origin"] for c in listed} == {"flywheel"} and len(listed) == 2, \
        "both policy lines it contradicts are on the list, although wording alone does not flag them"
    _, events = ask(login("customer@leon.demo"), "退款多久能到账啊")
    assert by_type(events, "final")[0]["status"] == "handoff"

    with new_session() as db:
        patch_doc = db.execute(select(Document).where(Document.title == "客服纠错知识补丁")).scalar_one()
        ingest_document(db, tenant_id=patch_doc.tenant_id, title=patch_doc.title, content=patch_doc.content,
                        embedder=get_services().embedder, source=patch_doc.source, document=patch_doc)
    assert len(client.get("/api/kb/conflicts", headers=admin).json()) == 2, "re-indexing the patch keeps them"

    with new_session() as db:  # the policy is changed to the corrected rule
        policy = db.execute(select(Document).where(Document.title == "售后与退款政策")).scalar_one()
        content = policy.content
        for sentence in {c["other_sentence"] for c in listed}:
            assert sentence in content
            content = content.replace(sentence, "退款审核通过后3天内原路到账。")
        ingest_document(db, tenant_id=policy.tenant_id, title=policy.title, content=content,
                        embedder=get_services().embedder, source=policy.source, document=policy)
    assert client.get("/api/kb/conflicts", headers=admin).json() == []
    resolved = client.get("/api/kb/conflicts?status_filter=resolved", headers=admin).json()
    assert len(resolved) == 2 and all("已修改" in c["note"] for c in resolved)
    _, events = ask(login("customer@leon.demo"), "退款多久能到账啊")
    assert by_type(events, "final")[0]["status"] == "completed"
