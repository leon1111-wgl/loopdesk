from app.agent.answer_cache import normalize_question
from app.db import new_session
from app.models import Run
from tests.conftest import by_type


def _done(events):
    """Routing and usage of the run, from its record: a customer's events do not carry them."""
    with new_session() as db:
        run = db.get(Run, by_type(events, "done")[0]["run_id"])
        return {"run_id": run.id, "route_tier": run.route_tier, "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens}


def test_question_normalisation():
    assert normalize_question("退款多久能到账？") == normalize_question(" 退款 多久能到账 ?")
    assert normalize_question("ＳＯ２０２６") == "so2026"


def test_repeated_faq_is_served_from_cache_with_its_evidence(client, ask, login):
    customer = login("customer@leon.demo")
    _, first = ask(customer, "退款多久能到账？")
    assert _done(first)["route_tier"] != "cache"
    _, second = ask(customer, "退款多久能到账?")  # different punctuation, same question
    done = _done(second)
    assert done["route_tier"] == "cache" and done["prompt_tokens"] == 0 and done["completion_tokens"] == 0
    assert not by_type(second, "tool_call"), "no retrieval or model call on a hit"
    assert by_type(second, "final")[0]["text"] == by_type(first, "final")[0]["text"]
    assert by_type(second, "verification")[0]["verdict"] == "pass"
    meta = by_type(second, "final")[0]["meta"]
    assert meta["cached"] is True and meta["citations"][0]["title"] == "售后与退款政策"
    trace = client.get(f"/api/runs/{done['run_id']}", headers=login("admin@leon.demo")).json()
    assert [s["kind"] for s in trace["spans"]] == ["guardrail", "cache"]


def test_knowledge_change_invalidates_cache(client, ask, login):
    customer = login("customer@leon.demo")
    ask(customer, "积分可以怎么抵扣？")
    client.post("/api/kb/documents/text", json={"title": "积分补充", "content": "## 积分\n- 积分不能兑换现金。"},
                headers=login("admin@leon.demo"))
    _, again = ask(customer, "积分可以怎么抵扣？")
    assert _done(again)["route_tier"] != "cache", "a new knowledge version must not replay old answers"


def test_cache_is_scoped_by_visibility(ask, login):
    _, staff = ask(login("agent@leon.demo"), "一线客服最多可以发放多少钱的补偿券？")
    assert "20元" in by_type(staff, "final")[0]["text"]
    _, customer = ask(login("customer@leon.demo"), "一线客服最多可以发放多少钱的补偿券？")
    assert _done(customer)["route_tier"] != "cache"
    assert "20元" not in by_type(customer, "final")[0]["text"], "internal answers never leak through the cache"


def test_tenants_do_not_share_cache(ask, login):
    ask(login("customer@leon.demo"), "七天无理由退货有什么条件？")
    _, other = ask(login("customer@iris.demo"), "七天无理由退货有什么条件？")
    assert _done(other)["route_tier"] != "cache"


def test_not_cached_follow_ups_orders_or_pii(ask, login):
    customer = login("customer@leon.demo")
    conversation_id, _ = ask(customer, "付款以后多久发货？")
    _, follow_up = ask(customer, "付款以后多久发货？", conversation_id=conversation_id)
    assert _done(follow_up)["route_tier"] != "cache", "turns with history depend on context"
    ask(customer, "SO20261008002 的快递到哪了？")
    _, orders = ask(customer, "SO20261008002 的快递到哪了？")
    assert _done(orders)["route_tier"] != "cache", "answers built from live business data are never cached"
    ask(customer, "我的手机号13812345678，发票怎么开？")
    _, pii = ask(customer, "我的手机号13812345678，发票怎么开？")
    assert _done(pii)["route_tier"] != "cache"


def test_cache_can_be_disabled(settings, ask, login):
    settings.answer_cache_ttl_hours = 0
    customer = login("customer@leon.demo")
    ask(customer, "退款多久能到账？")
    _, again = ask(customer, "退款多久能到账？")
    assert _done(again)["route_tier"] != "cache"


def test_overview_reports_cache_hit_rate(client, ask, login):
    customer = login("customer@leon.demo")
    ask(customer, "退款多久能到账？")
    ask(customer, "退款多久能到账？")
    overview = client.get("/api/metrics/overview", headers=login("admin@leon.demo")).json()
    assert overview["cache_hits"] == 1 and overview["cache_hit_rate"] == 0.5


def test_an_answer_given_after_a_failed_search_is_not_cached(client, ask, login, monkeypatch):
    # Code review: when search_knowledge failed (an exception in retrieval), the agent answered
    # "知识库中没有找到…", the run passed verification and the refusal was cached for a day.
    from app.agent import business_tools

    def broken(*args, **kwargs):
        raise ValueError("matmul: dimension mismatch")

    customer = login("customer@leon.demo")
    monkeypatch.setattr(business_tools, "hybrid_search", broken)
    ask(customer, "退款多久能到账？")
    monkeypatch.undo()
    _, again = ask(customer, "退款多久能到账？")
    assert _done(again)["route_tier"] != "cache", "the refusal from a failed search was replayed"
    assert "1-3个工作日" in by_type(again, "final")[0]["text"]


def test_a_different_embedding_model_does_not_reuse_cached_answers(settings):
    from app.agent.answer_cache import config_fingerprint

    before = config_fingerprint(settings)
    settings.embedding_dim = 1024
    assert config_fingerprint(settings) != before
