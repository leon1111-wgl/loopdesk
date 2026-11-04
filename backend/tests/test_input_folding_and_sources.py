"""What a question is read as, and what an answer rests on (10-28 ①, 10-29 ② and ④).

- N-221: "退款多久到賬" (traditional 賬) was answered with the review rules; only a traditional-script tenant's input
  was folded to simplified. Every tenant's is now, in the runtime, so every channel gets it.
- N-233: "积分十二个月就过期吗" was refused where "积分12个月就过期吗" was answered; "退款要七天吗" got the seven-day
  return policy. A Chinese numeral before a unit is read in digits too.
- N-226: a model that searched the same words six times left six citations of one chunk under the handoff.
- N-225: a model without a price cost ¥0 on the dashboard, with no word anywhere.
- N-234: a length cut went through words ("售|后") and the offline answer quoted "…空调安装费为每" as a fact.
"""

import logging

import pytest
from opencc import OpenCC

from app.config import Settings
from app.main import check_prices
from app.rag.chunking import CUT_MARK, chunk_markdown
from app.services import get_services
from app.textutil import canonical_query, expand_query, numeral_counts
from tests.conftest import by_type
from tests.scripted_model import ScriptedModel, call


def _final(events):
    return by_type(events, "final")[0]


# ---------------------------------------------------------------- N-221
def test_traditional_input_is_answered_like_simplified(ask, login):
    emma = login("customer@leon.demo")
    _, simplified = ask(emma, "退款多久到账")
    _, traditional = ask(emma, "退款多久到賬")
    assert "1-3个工作日到账" in _final(traditional)["text"]
    assert _final(traditional)["text"] == _final(simplified)["text"]


def test_a_traditional_two_part_question_tracks_the_parcel_and_answers_the_timing(ask, login):
    _, events = ask(login("customer@leon.demo"), "我的訂單SO20261008002到哪了 退款多久到賬")
    tools = [e["name"] for e in by_type(events, "tool_call")]
    assert "track_shipment" in tools and "request_refund" not in tools
    text = _final(events)["text"]
    assert "AT1402938475" in text and "1-3个工作日到账" in text and "申请退款" not in text


def test_folding_leaves_simplified_text_alone():
    fold = OpenCC("t2s").convert
    for text in ("七天无理由退货", "退款审核通过后，将按原支付方式原路退回", "发票抬头可以修改一次", "SO20261008002 到哪了"):
        assert fold(text) == text


# ---------------------------------------------------------------- N-233
@pytest.mark.parametrize("chinese,digits", [("积分十二个月就过期吗", "积分12个月就过期吗"), ("退款三天能到吗", "退款3天能到吗")])
def test_a_chinese_numeral_is_answered_like_digits(ask, login, chinese, digits):
    emma = login("customer@leon.demo")
    _, a = ask(emma, chinese)
    _, b = ask(emma, digits)
    assert "知识库中没有找到" not in _final(a)["text"]
    assert _final(a)["text"] == _final(b)["text"]


def test_how_long_a_refund_takes_is_not_the_seven_day_policy(ask, login):
    _, events = ask(login("customer@leon.demo"), "退款要七天吗")
    assert _final(events)["text"].startswith("微信支付、支付宝退款预计1-3个工作日到账")


@pytest.mark.parametrize("text", ["退款期限是七天吗", "退款七天内可以吗", "退款超过七天还行吗"])
def test_a_refund_deadline_question_is_not_read_as_arrival_time(text):  # second-model review P1
    assert "到账 工作日" not in expand_query(text) and "到账 工作日" not in canonical_query(text)


def test_numerals_are_read_only_before_a_unit():
    assert numeral_counts("积分十二个月就过期吗 两天了") == "积分12个月就过期吗 2天了"
    assert numeral_counts("十分满意 我等了一下午 第一单") == "十分满意 我等了一下午 第一单"
    for text in ("万一天气不好", "三五天", "两三天", "三四个工作日", "万元"):  # ranges and 万一 are not one number
        assert numeral_counts(text) == text
    assert expand_query("积分十二个月就过期吗").endswith(" 12个月")  # expanded, never replaced
    assert canonical_query("积分十二个月就过期吗") == "积分十二个月就过期吗 12个月"
    assert canonical_query("二次销售是什么意思") == "二次销售是什么意思"  # 次 is not read: the knowledge base says 一次 and 二次 itself


# ---------------------------------------------------------------- N-226
def test_the_same_chunk_found_again_is_one_citation(ask, login, monkeypatch):
    monkeypatch.setattr(get_services(), "provider", ScriptedModel(call("search_knowledge", query="退款多久到账")))
    _, events = ask(login("customer@leon.demo"), "退款多久到账")
    final = _final(events)
    assert final["status"] == "handoff" and final["meta"]["citations"] == [], "a handoff sentence quotes nothing"
    refs = [r["ref"] for e in by_type(events, "tool_result") for r in e["output"]["results"]]
    assert len(refs) > len(set(refs)) and len(set(refs)) <= 4, "searched again, the chunk keeps its first ref"


def test_two_searches_hitting_one_chunk_cite_it_once(ask, login, monkeypatch):
    monkeypatch.setattr(get_services(), "provider", ScriptedModel(
        call("search_knowledge", query="退款多久到账"), call("search_knowledge", query="退款到账时间"),
        "微信支付、支付宝退款预计1-3个工作日到账[1]。"))
    _, events = ask(login("customer@leon.demo"), "退款多久到账")
    chunks = [c["meta"]["chunk_id"] for c in _final(events)["meta"]["citations"]]
    assert chunks and len(chunks) == len(set(chunks))


# ---------------------------------------------------------------- N-225
def test_an_unpriced_model_is_named_not_shown_as_free(client, login, ask, caplog):
    settings = get_services().settings
    settings.llm_model_strong = "deepseek-chat"
    with caplog.at_level(logging.WARNING, logger="app.main"):
        check_prices(settings)
    assert "deepseek-chat 没有配置价格" in caplog.text
    assert client.get("/api/health").json()["unpriced_models"] == ["deepseek-chat"]
    ask(login("customer@leon.demo"), "退款多久到账")
    overview = client.get("/api/metrics/overview?days=7", headers=login("admin@leon.demo")).json()
    assert overview["cost_unpriced_models"] == ["deepseek-chat"]


def test_priced_models_raise_nothing(caplog):
    settings = Settings()
    assert settings.unpriced_models() == []
    with caplog.at_level(logging.WARNING, logger="app.main"):
        check_prices(settings)
    assert "没有配置价格" not in caplog.text


# ---------------------------------------------------------------- N-234
PAD = ("本店售后服务方式包括上门取件以及快递寄回等多种渠道顾客可以根据自己的情况选择合适的方式我们会尽快为您处理" * 6)[:290]
PRICE = "空调安装费为每台199元含两米铜管超出部分每米收费50元"


def test_a_length_cut_falls_between_words_and_is_marked():
    import jieba

    chunks = chunk_markdown("# 安装服务\n\n" + PAD + PRICE + "售后服务方式包括上门取件和快递寄回")
    assert len(chunks) == 2 and max(len(c.text) for c in chunks) <= 300
    first, second = chunks[0].text, chunks[1].text
    assert first.endswith(CUT_MARK) and second.startswith(CUT_MARK)
    whole = PAD + PRICE
    words = {end for _, _, end in jieba.tokenize(whole)}
    cut = whole.index(first.removesuffix(CUT_MARK)[-12:]) + 12
    assert cut in words, first[-14:]
    assert not first.removesuffix(CUT_MARK).endswith("为每")


def test_the_offline_answer_does_not_quote_a_cut_off_tail(client, login):
    admin = login("admin@leon.demo")
    client.post("/api/kb/documents/text", headers=admin, json={
        "title": "安装服务", "content": "# 安装服务\n\n" + PAD + PRICE + "售后服务方式包括上门取件和快递寄回"})
    answer = client.post("/api/chat", json={"content": "空调安装费多少钱"}, headers=login("customer2@leon.demo")).json()["answer"]
    assert "每台199元" in answer and f"{CUT_MARK}[" not in answer and f"{CUT_MARK}。" not in answer
    assert not answer.startswith(("为您", "您处理")) or answer.startswith(CUT_MARK), "a fragment is shown as one"
    assert not any(s.rstrip("[]0123456789").endswith(("为每", "为", "每")) for s in answer.split("。") if s), answer
    assert len(answer) < 200, len(answer)
