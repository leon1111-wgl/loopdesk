from tests.conftest import by_type


def test_correction_becomes_eval_case_and_kb_patch_and_closes_the_loop(client, ask, login):
    customer = login("customer@leon.demo")
    agent = login("agent@leon.demo")
    admin = login("admin@leon.demo")

    # 1. knowledge gap: the agent refuses
    conversation_id, events = ask(customer, "以旧换新活动怎么参加？")
    message_id = by_type(events, "final")[0]["message_id"]
    assert "没有找到" in by_type(events, "final")[0]["text"]

    # customers may rate but not correct
    assert client.post(f"/api/messages/{message_id}/feedback", json={"rating": "correction", "correction": "x"},
                       headers=customer).status_code == 400

    # 2. a support agent writes the correct answer
    correction = "以旧换新活动仅限手机和平板品类，在商品页点击“以旧换新”提交旧机信息，审核通过后可抵扣50-800元。"
    r = client.post(f"/api/messages/{message_id}/feedback",
                    json={"rating": "correction", "correction": correction, "reason_category": "knowledge_gap"},
                    headers=agent).json()
    assert r["attribution"] == "knowledge_gap"
    assert r["eval_case_id"] and r["patch_id"]

    cases = client.get("/api/evals/cases", headers=admin).json()
    new_case = next(c for c in cases if c["id"] == r["eval_case_id"])
    assert new_case["status"] == "candidate" and new_case["source"] == "flywheel"
    assert "800元" in new_case["must_include"]

    # 3. admin applies the patch -> knowledge base re-indexed, case activated
    applied = client.post(f"/api/flywheel/patches/{r['patch_id']}/apply", headers=admin).json()
    assert applied["status"] == "applied"
    assert client.post(f"/api/flywheel/patches/{r['patch_id']}/apply", headers=admin).status_code == 409
    cases = client.get("/api/evals/cases", headers=admin).json()
    assert next(c for c in cases if c["id"] == r["eval_case_id"])["status"] == "active"

    # 4. the same question is now answered from the patch, with a citation
    _, events = ask(customer, "以旧换新活动怎么参加？")
    final = by_type(events, "final")[0]
    assert "800元" in final["text"]
    assert final["meta"]["citations"][0]["title"] == "客服纠错知识补丁"

    # 5. regression suite: the new case passes and nothing else broke
    run = client.post("/api/evals/runs", json={}, headers=admin).json()
    active = [c for c in client.get("/api/evals/cases", headers=admin).json()
              if c["status"] == "active" and c["suite"] == "default"]
    assert run["passed"] == run["total"] == len(active), "the new case passes and nothing else regressed"
    detail = client.get(f"/api/evals/runs/{run['id']}", headers=admin).json()
    assert any(res["case_key"] == new_case["key"] and res["passed"] for res in detail["results"])

    stats = client.get("/api/flywheel/stats", headers=admin).json()
    assert stats["eval_cases"]["from_flywheel"] == 1 and stats["patches"] == {"applied": 1}


def test_eval_suite_detects_regressions(settings, client, login):
    admin = login("admin@leon.demo")
    first = client.post("/api/evals/runs", json={}, headers=admin).json()
    assert first["metrics"]["pass_rate"] == 1.0
    m = first["metrics"]  # input and output are priced apart, so the cost estimate needs both (W7)
    assert m["prompt_tokens"] > 0 and m["completion_tokens"] > 0
    assert m["prompt_tokens"] + m["completion_tokens"] == m["total_tokens"]

    # Simulate a bad change: shrink the auto-refund limit so a previously automatic refund now needs approval.
    client.patch("/api/tenant", json={"auto_refund_limit": 10}, headers=admin)
    second = client.post("/api/evals/runs", json={}, headers=admin).json()
    assert "refund_auto" in second["metrics"]["diff"]["regressions"]
    assert second["metrics"]["pass_rate"] < 1.0


def test_thumbs_down_is_attributed(client, ask, login):
    customer = login("customer@leon.demo")
    _, events = ask(customer, "你们老板的私人手机号是多少？")
    message_id = by_type(events, "final")[0]["message_id"]
    r = client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down"}, headers=customer).json()
    assert r["attribution"] == "knowledge_gap" and r["eval_case_id"] is None


def test_eval_runs_in_sandbox_and_ignores_live_order_changes(client, ask, login):
    from app.db import new_session
    from app.models import Order, Run

    # a real refund changes live data ...
    ask(login("customer@leon.demo"), "订单 SO20260918001 申请退款")
    with new_session() as db:
        assert db.get(Order, "SO20260918001").status == "refunding"
    # ... but the regression suite still sees the fixture state
    run = client.post("/api/evals/runs", json={}, headers=login("admin@leon.demo")).json()
    assert run["metrics"]["pass_rate"] == 1.0
    with new_session() as db:
        assert db.get(Order, "SO20260918001").status == "refunding", "eval must not touch live data"
        assert db.query(Run).filter(Run.is_eval.is_(True)).count() == run["total"], "eval traces are kept"


def test_a_second_rating_changes_the_first_instead_of_counting_twice(client, ask, login):
    customer, admin = login("customer@leon.demo"), login("admin@leon.demo")
    _, events = ask(customer, "退款多久能到账？")
    message_id = by_type(events, "final")[0]["message_id"]
    first = client.post(f"/api/messages/{message_id}/feedback", json={"rating": "up"}, headers=customer).json()
    second = client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down"}, headers=customer).json()
    client.post(f"/api/messages/{message_id}/feedback", json={"rating": "down"}, headers=customer)
    assert first["feedback_id"] == second["feedback_id"]
    assert client.get("/api/flywheel/stats", headers=admin).json()["feedback"] == {"down": 1}


def test_the_same_correction_sent_twice_is_one_correction(client, ask, login):
    from sqlalchemy import func, select

    from app.db import new_session
    from app.models import EvalCase, KBPatch

    _, events = ask(login("customer@leon.demo"), "以旧换新活动怎么参加？")
    message_id = by_type(events, "final")[0]["message_id"]
    agent = login("agent@leon.demo")
    body = {"rating": "correction", "reason_category": "knowledge_gap",
            "correction": "以旧换新活动仅限手机和平板品类，审核通过后可抵扣50-800元。"}
    first = client.post(f"/api/messages/{message_id}/feedback", json=body, headers=agent).json()
    again = client.post(f"/api/messages/{message_id}/feedback", json=body, headers=agent).json()
    assert again == first
    other = client.post(f"/api/messages/{message_id}/feedback", headers=agent,
                        json={**body, "correction": "以旧换新仅限手机品类。"}).json()
    assert other["feedback_id"] != first["feedback_id"], "a different correction is a new one"
    with new_session() as db:
        assert db.execute(select(func.count(EvalCase.id)).where(EvalCase.source == "flywheel")).scalar_one() == 2
        assert db.execute(select(func.count(KBPatch.id))).scalar_one() == 2


def test_concurrent_identical_ratings_store_one(client, ask, login):
    import threading

    from sqlalchemy import func, select

    from app.db import new_session
    from app.models import Feedback

    customer = login("customer@leon.demo")
    _, events = ask(customer, "退款多久能到账？")
    message_id = by_type(events, "final")[0]["message_id"]
    barrier, codes = threading.Barrier(4, timeout=10), []

    def click():
        barrier.wait()
        codes.append(client.post(f"/api/messages/{message_id}/feedback", json={"rating": "up"}, headers=customer).status_code)

    threads = [threading.Thread(target=click) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert codes == [200] * 4
    with new_session() as db:
        assert db.execute(select(func.count(Feedback.id)).where(Feedback.message_id == message_id)).scalar_one() == 1


def test_a_customer_does_not_see_the_agents_correction_of_their_answer(client, ask, login):
    customer = login("customer@leon.demo")
    conversation_id, events = ask(customer, "以旧换新活动怎么参加？")
    message_id = by_type(events, "final")[0]["message_id"]
    client.post(f"/api/messages/{message_id}/feedback", headers=login("agent@leon.demo"), json={
        "rating": "correction", "reason_category": "knowledge_gap", "correction": "内部口径：仅限手机品类。"})
    listed = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert all(m["feedback"] is None for m in listed)
    assert "内部口径" not in str(listed)


def test_a_correction_retried_after_a_failed_draft_still_creates_its_case_and_patch(client, ask, login, monkeypatch):
    # Code review: the feedback row (with its de-duplication key) was committed before the patch
    # was drafted. When drafting failed, the retry found the row and returned no case and no
    # patch, and the correction was lost unless someone reworded it.
    from sqlalchemy import func, select

    from app.db import new_session
    from app.models import EvalCase, Feedback, KBPatch
    from app.services import get_services

    _, events = ask(login("customer@leon.demo"), "以旧换新活动怎么参加？")
    message_id = by_type(events, "final")[0]["message_id"]
    agent = login("agent@leon.demo")
    body = {"rating": "correction", "reason_category": "knowledge_gap",
            "correction": "以旧换新活动仅限手机和平板品类，审核通过后可抵扣50-800元。"}
    provider, calls = get_services().provider, []
    draft = provider.chat

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("upstream 503")
        return draft(*args, **kwargs)

    monkeypatch.setattr(provider, "chat", flaky)
    assert client.post(f"/api/messages/{message_id}/feedback", json=body, headers=agent).status_code == 500
    retried = client.post(f"/api/messages/{message_id}/feedback", json=body, headers=agent).json()
    assert retried["eval_case_id"] and retried["patch_id"]
    with new_session() as db:
        assert db.execute(select(func.count(Feedback.id)).where(Feedback.rating == "correction")).scalar_one() == 1
        assert db.execute(select(func.count(EvalCase.id)).where(EvalCase.source == "flywheel")).scalar_one() == 1
        assert db.execute(select(func.count(KBPatch.id))).scalar_one() == 1
