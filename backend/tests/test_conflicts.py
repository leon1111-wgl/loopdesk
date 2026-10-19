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


def test_editing_a_document_reindexes_it_and_rechecks_its_conflicts(client, ask, login):
    admin = login("admin@leon.demo")
    created = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    assert created["conflicts"] == 1
    edited = client.put(f"/api/kb/documents/{created['id']}", headers=admin, json={
        "title": "到账补充说明", "content": "## 到账\n- 微信支付、支付宝退款预计1-3个工作日到账，节假日顺延。",
        "visibility": "public", "version": created["version"]}).json()
    assert edited["version"] == created["version"] + 1 and edited["conflicts"] == 0
    assert client.get("/api/kb/conflicts", headers=admin).json() == []
    hits = client.post("/api/kb/search", json={"query": "节假日顺延"}, headers=admin).json()["hits"]
    assert any("节假日顺延" in h["text"] for h in hits), "the new text is searchable"
    _, events = ask(login("customer@leon.demo"), "微信退款多久能到账？")
    assert by_type(events, "final")[0]["status"] == "completed"


def test_an_edit_made_on_an_old_version_is_refused_instead_of_overwriting(client, login):
    admin = login("admin@leon.demo")
    doc = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    body = {"title": "到账补充说明", "visibility": "public", "version": doc["version"]}
    first = client.put(f"/api/kb/documents/{doc['id']}", json={**body, "content": "第一位管理员的修改。"}, headers=admin)
    second = client.put(f"/api/kb/documents/{doc['id']}", json={**body, "content": "第二位管理员的修改。"}, headers=admin)
    assert first.status_code == 200 and second.status_code == 409
    assert "第一位" in client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()["content"]
    assert client.put(f"/api/kb/documents/{doc['id']}", json={**body, "content": "x"},
                      headers=login("agent@leon.demo")).status_code == 403


def test_concurrent_edits_of_one_version_save_exactly_once(client, login):
    import threading

    from app.db import new_session
    from app.models import Document
    from app.rag.ingest import StaleDocument, edit_document
    from app.services import get_services

    admin = login("admin@leon.demo")
    doc = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    barrier, results = threading.Barrier(2, timeout=10), []

    def save(text):
        with new_session() as db:
            document = db.get(Document, doc["id"])
            barrier.wait()
            try:
                edit_document(db, document, title=document.title, content=text, visibility="public",
                              embedder=get_services().embedder, base_version=doc["version"])
                results.append("saved")
            except StaleDocument:
                results.append("stale")

    threads = [threading.Thread(target=save, args=(f"管理员{i}的修改。",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["saved", "stale"]
    assert client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()["version"] == doc["version"] + 1


def test_the_dashboard_shows_the_conflict_backlog_and_what_it_costs(client, ask, login):
    admin = login("admin@leon.demo")
    assert client.get("/api/metrics/overview", headers=admin).json()["knowledge_conflicts"] == \
        {"open": 0, "oldest_open_s": 0, "handoffs": 0}
    client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin)
    ask(login("customer@leon.demo"), "微信退款多久能到账？")
    backlog = client.get("/api/metrics/overview", headers=admin).json()["knowledge_conflicts"]
    assert backlog["open"] == 1 and backlog["handoffs"] == 1 and backlog["oldest_open_s"] >= 0


def test_a_resolved_conflict_stays_resolved_when_either_document_is_reindexed(client, ask, login):
    # Code review: re-detection on every re-index ignored resolved pairs, so an unrelated edit (or
    # any flywheel publish, which re-indexes the patch document) reopened a conflict an
    # administrator had settled, and customers were handed to a person again.
    admin = login("admin@leon.demo")
    doc = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    conflict_id = client.get("/api/kb/conflicts", headers=admin).json()[0]["id"]
    client.post(f"/api/kb/conflicts/{conflict_id}/resolve", json={"note": "补充说明为准"}, headers=admin)
    current = client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()
    client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={
        "title": current["title"], "content": current["content"] + "\n\n如有疑问请联系客服。",
        "visibility": "public", "version": current["version"]})
    assert client.get("/api/kb/conflicts", headers=admin).json() == []
    _, events = ask(login("customer@leon.demo"), "微信退款多久能到账？")
    assert by_type(events, "final")[0]["status"] == "completed"
    # a changed sentence is a new claim: it is checked again
    current = client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()
    client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={
        "title": current["title"], "content": current["content"].replace("1-2个工作日", "1个工作日"),
        "visibility": "public", "version": current["version"]})
    assert len(client.get("/api/kb/conflicts", headers=admin).json()) == 1


def test_a_document_edit_does_not_stall_the_tenants_chats_on_postgresql(settings, client, login):
    # Code review: the edit locked the tenant row FOR UPDATE until re-indexing committed. Every
    # insert that references the tenant (runs, messages, spans) needs FOR KEY SHARE on that row,
    # which FOR UPDATE blocks, so chats waited for the edit (a remote embedder, a large document).
    # FOR NO KEY UPDATE still serialises edits with flywheel publishing but lets them through.
    import threading

    import pytest
    from sqlalchemy import select, text

    from app.db import new_session
    from app.models import Document, Run, Tenant, User
    from app.rag.ingest import edit_document
    from app.services import get_services

    if not settings.database_url.startswith("postgresql"):
        pytest.skip("SQLite has one writer by design; the row-lock modes are PostgreSQL's")
    admin = login("admin@leon.demo")
    doc = client.post("/api/kb/documents/text", json=CONFLICTING, headers=admin).json()
    real, embedding, release = get_services().embedder, threading.Event(), threading.Event()

    class SlowEmbedder:  # a remote embedding call that takes a while
        def embed(self, texts):
            embedding.set()
            release.wait(10)
            return real.embed(texts)

    def edit():
        with new_session() as db:
            document = db.get(Document, doc["id"])
            edit_document(db, document, title=document.title, content=document.content + "\n- 节假日顺延。",
                          visibility="public", embedder=SlowEmbedder(), base_version=document.version)

    editor = threading.Thread(target=edit)
    editor.start()
    try:
        assert embedding.wait(10)
        with new_session() as db:  # a chat request of the same tenant starts a run meanwhile
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
            user = db.execute(select(User).where(User.tenant_id == tenant.id)).scalars().first()
            db.add(Run(tenant_id=tenant.id, user_id=user.id))
            db.commit()
    finally:
        release.set()
        editor.join()
