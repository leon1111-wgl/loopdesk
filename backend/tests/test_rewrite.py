from app.agent.rewrite import is_followup, rule_rewrite
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
