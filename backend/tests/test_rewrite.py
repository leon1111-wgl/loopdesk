from app.agent.rewrite import is_followup, needs_context, rule_rewrite
from tests.conftest import by_type


def test_followup_detection_is_explicit():
    previous = "七天无理由退货有什么条件？"
    assert is_followup("那运费谁出？", previous)
    assert is_followup("银行卡呢？", previous)
    assert not is_followup("积分可以怎么抵扣？", previous), "short but self-contained"
    assert not is_followup("那 SO20261008002 到哪了", previous), "an order number is self-contained"
    assert not is_followup("那运费谁出？", None), "nothing to follow up on"


def test_rule_rewrite_keeps_the_previous_topic():
    assert rule_rewrite("那运费谁出？", "七天无理由退货有什么条件？") == "七天无理由退货条件，运费谁出？"


def test_followup_is_answered_in_context_and_traced(client, ask, login):
    customer = login("customer@leon.demo")
    conversation_id, _ = ask(customer, "七天无理由退货有什么条件？")
    _, events = ask(customer, "那运费谁出？", conversation_id=conversation_id)
    assert by_type(events, "rewrite")[0]["standalone"].startswith("七天无理由退货")
    assert "买家承担" in by_type(events, "final")[0]["text"]
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=customer).json()["messages"]
    assert messages[-2]["content"] == "那运费谁出？", "the conversation keeps what the customer typed"
    run_id = by_type(events, "done")[0]["run_id"]
    spans = client.get(f"/api/runs/{run_id}", headers=login("admin@leon.demo")).json()["spans"]
    rewrite = next(s for s in spans if s["kind"] == "rewrite")
    assert rewrite["output"]["method"] == "rule"


def test_an_additive_opener_with_a_complete_question_starts_a_new_topic():
    assert not needs_context("还有能开发票吗")
    assert rule_rewrite("还有能开发票吗", "那多久能到呢") == "能开发票吗"
    assert needs_context("还有积分呢？"), "a bare 呢 still leans on the previous question"
    assert needs_context("那运费谁出？")


def test_a_bare_followup_swaps_the_subject_and_keeps_the_question():
    from app.agent.rewrite import frame_of, next_frame

    root = frame_of("七天无理由退货有什么条件？")
    second = next_frame(root, "那运费谁出？", rule_rewrite("那运费谁出？", root))
    assert (second.topic, second.predicate) == ("七天无理由退货条件", "运费谁出")
    assert rule_rewrite("那质量问题呢？", second) == "质量问题，运费谁出"
    assert rule_rewrite("那银行卡呢", frame_of("退款多久到账")) == "银行卡，退款到账"


def test_chained_followups_build_on_the_previous_rewrite(ask, login):
    customer = login("customer@leon.demo")
    conversation_id, _ = ask(customer, "七天无理由退货有什么条件？")
    ask(customer, "那运费谁出？", conversation_id=conversation_id)
    _, events = ask(customer, "那质量问题呢？", conversation_id=conversation_id)
    assert by_type(events, "rewrite")[0]["standalone"] == "质量问题，运费谁出", "built on turn 2, not on what was typed"
    assert "往返运费由Leon优选承担" in by_type(events, "final")[0]["text"]


def test_a_new_question_after_a_followup_is_not_glued_to_it(ask, login):
    customer = login("customer@leon.demo")
    conversation_id, _ = ask(customer, "付款以后多久发货？")
    ask(customer, "那多久能到呢", conversation_id=conversation_id)
    _, events = ask(customer, "还有能开发票吗", conversation_id=conversation_id)
    assert by_type(events, "rewrite")[0]["standalone"] == "能开发票吗"
    assert "电子普通发票" in by_type(events, "final")[0]["text"]
