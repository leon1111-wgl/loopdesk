"""The tiered answer check (LOOPDESK_VERIFIER=tiered): claims only, rules first, the judge only where the
cheap tiers disagree, and FIX / one REASK / handoff for an answer that fails.

The lexical check failed 5.8% of the correct claims in the recorded real-model answers and missed 21 of
44 wrong ones; on sentences planted wrong by another model it caught 61 of 126. Most misses kept nearly
every word of the evidence and changed one: who pays, which plan, 下单后 instead of 签收后, 所有 instead of
仅管理员 (docs/EVALUATION.md, "可信回答").
"""

import json
import re

import pytest

from app.agent.grounding import flags, ground
from app.agent.tools import Evidence
from app.agent.verifier import REASK_MARKER, plan_repair, reask_message, verify_answer
from app.config import Settings
from app.llm.base import ChatResult, LLMProvider, Usage
from app.llm import mock


def settings(**values) -> Settings:
    s = Settings()
    s.verifier = "tiered"
    for k, v in values.items():
        setattr(s, k, v)
    return s


def kb(ref: int, text: str) -> Evidence:
    return Evidence(ref, "kb", "政策", text)


def tool(ref: int, name: str, output: dict) -> Evidence:
    return Evidence(ref, "tool", name, json.dumps(output, ensure_ascii=False))


RETURNS = kb(1, "签收后7天内，商品完好、不影响二次销售的，可申请七天无理由退货。无理由退货的寄回运费由买家承担；"
                "质量问题的往返运费由我们承担。定制商品不支持七天无理由退货。")
SHIPPING = kb(2, "默认使用 Atlas 快运和 Orion 快递，暂不支持指定快递公司。现货商品付款后 48 小时内发货。")
PLANS = kb(3, "专业版提供 1TB 存储空间，免费版提供 5GB。仅团队管理员可以在「团队设置-数据导出」中导出全部文档。"
              "发票开具后 30 天内可修改一次抬头。P1 级故障 1 小时内响应。补偿券每个客户每月限 1 次。")
REFUND_TIME = kb(4, "退款审核通过后按原支付方式原路退回，不能指定退到其他银行卡。微信支付、支付宝预计1-3个工作日到账。")


@pytest.mark.parametrize("claim, evidence, flag", [
    ("下单时可指定快递公司。", SHIPPING, "polarity"),
    ("无理由退货的寄回运费由商家承担。", RETURNS, "party"),
    ("免费版提供 1TB 存储空间。", PLANS, "entity"),
    ("下单后7天内可申请七天无理由退货。", RETURNS, "anchor"),
    ("任何团队成员都可以在「团队设置-数据导出」中导出全部文档。", PLANS, "scope"),
    ("发票开具后 30 天内可多次修改抬头。", PLANS, "count"),
    ("P1 级故障 1 个工作日内响应。", PLANS, "unit"),
    ("补偿券每个客户每周限 1 次。", PLANS, "period"),
    ("微信支付、支付宝预计1-3个小时到账。", REFUND_TIME, "unit"),
    ("退款保证当天到账。", REFUND_TIME, "promise"),
    ("订单付款后即可在「我的订单」中开具电子普通发票。", kb(5, "订单确认收货后，可在“我的订单”中开具电子普通发票。"), "anchor"),
    ("偏远地区的15元额外运费由我们承担。", kb(5, "新疆、西藏等偏远地区需额外支付15元运费。"), "party"),
    ("申请时无需上传商品问题照片。", kb(5, "申请质量问题退换时，请上传商品问题照片，客服会在24小时内完成审核。"), "polarity"),
    ("积分长期有效，不会过期清零。", kb(5, "积分有效期为获得之日起12个月，过期自动清零。"), "polarity"),
    ("现货和预售商品均在付款后48小时内发货。", kb(5, "现货商品在付款后48小时内发货，预售商品以商品页标注的发货时间为准。"), "scope"),
    ("免费版每个成员提供 5GB 存储空间。", kb(5, "免费版每个团队提供5GB存储空间，专业版提供1TB存储空间。"), "entity"),
])
def test_one_changed_word_flags_a_grounded_claim(claim, evidence, flag):
    found = flags(claim, evidence.text)
    assert any(f.startswith(flag) for f in found), found


@pytest.mark.parametrize("claim, evidence", [
    ("退款按原支付方式原路退回，无法指定其他银行卡。", REFUND_TIME),
    ("签收后7天内可以申请七天无理由退货，寄回运费由买家承担。", RETURNS),
    ("质量问题的往返运费由我们承担。", RETURNS),
    ("定制商品不在七天无理由退货的支持范围内。", RETURNS),
    ("专业版提供 1TB 存储空间。", PLANS),
    ("现有规则是：现货商品在付款后48小时内发货。", SHIPPING),
])
def test_a_faithful_paraphrase_is_not_flagged(claim, evidence):
    v = verify_answer(claim, [evidence], "问一下", settings())
    assert v.sentences[0].supported, (v.sentences[0].flags, v.sentences[0].score, v.sentences[0].clause_score)


def test_an_invented_clause_fails_a_grounded_sentence():
    shipment = tool(1, "track_shipment", {"order_id": "SO20261008002", "shipped": True,
                                          "events": [{"time": "2026-10-20 17:57", "desc": "派送中，快递员正在派件"}],
                                          "message": "最新进度（2026-10-20 17:57）：派送中，快递员正在派件。"})
    g = ground("派送中，快递员正在派件，预计很快送达。", [(1, shipment.text)], "到哪了", shipment.text)
    assert g.sentence_score >= 0.5 and g.clause_score < 0.5, g


def test_tool_fields_and_negation_wording_ground_the_claim():
    unshipped = tool(1, "track_shipment", {"order_id": "SO20261012003", "shipped": False,
                                           "message": "订单 SO20261012003 目前还未发货。"})
    v = verify_answer("您的订单目前尚未发货，暂无物流信息。", [unshipped], "SO20261012003 到哪了", settings())
    assert v.verdict == "pass", v.sentences
    two = [tool(1, "get_order", {"order_id": "SO20260918001", "refundable": True, "status_text": "已签收"}),
           tool(2, "get_order", {"order_id": "SO20261008002", "refundable": True, "status_text": "已发货"})]
    assert verify_answer("两单目前均可申请退款。", two, "SO20260918001 和 SO20261008002 能退吗", settings()).verdict == "pass"


def test_a_condition_before_a_claim_does_not_hide_it():
    v = verify_answer("如需加急，专票可在当天审核完成并寄出[1]。", [kb(1, "增值税专用发票的审核时效为3个工作日，审核通过后以快递寄出。")],
                      "专票多久", settings())
    assert v.sentences[0].factual and not v.sentences[0].supported


def test_an_order_number_is_always_checked():
    v = verify_answer("您好，订单 SO20261012099 的物流信息如下：", [SHIPPING], "SO20261012003 到哪了", settings())
    assert v.sentences[0].factual and v.sentences[0].unknown_ids == ["SO20261012099"]


# ---------------------------------------------------------------- the judge, only on disagreement
class CountingJudge(LLMProvider):
    name = "judge"

    def __init__(self, verdict: bool = True):
        self.calls, self.verdict, self.prompts = 0, verdict, []

    def chat(self, messages, tools, model, options=None):
        self.calls += 1
        self.prompts.append(messages[-1].content)
        n = len(re.findall(r"^\d+\. ", messages[-1].content.split("句子（按编号）：")[1], re.M))
        return ChatResult(json.dumps({"supported": [self.verdict] * n}), [], Usage(), model)


def test_the_judge_is_not_called_when_the_cheap_tiers_agree():
    judge = CountingJudge()
    v = verify_answer("签收后7天内可申请七天无理由退货[1]。请提供订单号。", [RETURNS], "能退吗", settings(), judge, "fast")
    assert v.verdict == "pass" and judge.calls == 0 and v.judge_calls == 0


def test_the_judge_reads_only_the_claims_the_tiers_disagree_on():
    judge = CountingJudge(verdict=True)
    answer = "签收后7天内可申请七天无理由退货[1]。包裹正在派送中，稍后会有专人与您联系[1]。"
    v = verify_answer(answer, [RETURNS], "能退吗", settings(), judge, "fast")
    assert judge.calls == 1 and v.judged == 1
    assert "签收后7天内" not in judge.prompts[0].split("句子（按编号）：")[1]
    assert v.verdict == "pass" and v.sentences[1].tier == "judge"
    # a number is never the judge's call
    v = verify_answer("签收后9天内可申请七天无理由退货[1]。", [RETURNS], "能退吗", settings(), CountingJudge(True), "fast")
    assert v.verdict == "blocked" and v.sentences[0].reason == "number"


def test_without_a_judge_a_disagreement_counts_as_unsupported():
    v = verify_answer("包裹正在派送中，稍后会有专人与您联系[1]。", [RETURNS], "能退吗", settings(verifier_judge=False))
    assert not v.sentences[0].supported and v.sentences[0].reason in ("unsure", "unsupported")


# ---------------------------------------------------------------- repair
def test_fix_drops_a_failed_claim_when_the_rest_still_answers():
    answer = "签收后7天内可申请七天无理由退货[1]。\n无理由退货的寄回运费由买家承担[1]。\n另外会员每月可享免费退货运费。"
    v = verify_answer(answer, [RETURNS], "能退吗", settings())
    repair = plan_repair(v, answer)
    assert repair.action == "fix" and repair.dropped == ["另外会员每月可享免费退货运费。"]
    assert repair.text == "签收后7天内可申请七天无理由退货[1]。\n无理由退货的寄回运费由买家承担[1]。"


def test_a_wrong_number_is_reasked_once_then_handed_over():
    answer = "签收后9天内可申请七天无理由退货[1]。无理由退货的寄回运费由买家承担[1]。"
    v = verify_answer(answer, [RETURNS], "能退吗", settings())
    assert plan_repair(v, answer).action == "reask", "a wrong number is not dropped silently"
    assert plan_repair(v, answer, reasked=True).action == "handoff"
    message = reask_message(plan_repair(v, answer))
    assert message.startswith(REASK_MARKER) and "数字 9" in message and "签收后9天内" in message


def test_an_answer_that_is_all_failure_is_reasked_not_emptied():
    answer = "本店所有商品终身免费保修。"
    assert plan_repair(verify_answer(answer, [RETURNS], "保修吗", settings()), answer).action == "reask"


def test_the_offline_model_reads_a_reask_as_a_correction_not_a_question():
    assert mock.REASK_MARKER == REASK_MARKER


# ---------------------------------------------------------------- in the agent loop
from sqlalchemy import select  # noqa: E402

from app.db import new_session  # noqa: E402
from app.models import Message, Ticket  # noqa: E402
from app.services import get_services  # noqa: E402
from tests.conftest import by_type  # noqa: E402
from tests.scripted_model import ScriptedModel, call  # noqa: E402

GOOD = "自签收之日起7天内，商品保持完好、不影响二次销售的，可申请七天无理由退货[1]。"


def _tiered(monkeypatch, *steps, judge: bool = False) -> ScriptedModel:
    model = ScriptedModel(*steps, judge=judge)
    services = get_services()
    monkeypatch.setattr(services, "provider", model)
    monkeypatch.setattr(services.settings, "verifier", "tiered")
    return model


def _meta(run_id: str) -> dict:
    with new_session() as db:
        return db.execute(select(Message).where(Message.run_id == run_id, Message.role == "assistant")
                          .order_by(Message.id.desc())).scalars().first().meta


def test_fix_drops_the_unsupported_sentence_and_keeps_the_answer(ask, login, monkeypatch):
    model = _tiered(monkeypatch, call("search_knowledge", query="七天无理由退货条件"),
                    GOOD + "另外，会员每月可享两次免费上门取件服务。", judge=False)
    _, events = ask(login("customer@leon.demo"), "七天无理由退货有什么条件？")
    final = by_type(events, "final")[0]
    assert final["text"] == GOOD and final["status"] == "completed"
    assert model.judged == 1, "the judge read the one claim the cheap tiers disagreed on"
    check = by_type(events, "verification")[0]
    assert check["verdict"] == "pass" and [s["dropped"] for s in check["sentences"]] == [False, True]
    assert _meta(final["run_id"])["repair"] == {"action": "fix", "dropped": ["另外，会员每月可享两次免费上门取件服务。"]}
    assert not by_type(events, "reask")


def test_a_wrong_number_is_reasked_once_and_the_correction_is_answered(ask, login, monkeypatch):
    model = _tiered(monkeypatch, call("search_knowledge", query="七天无理由退货条件"),
                    "自签收之日起9天内，商品保持完好的，可申请七天无理由退货[1]。", GOOD)
    _, events = ask(login("admin@leon.demo"), "七天无理由退货有什么条件？")
    assert by_type(events, "reask")[0]["sentences"] == ["自签收之日起9天内，商品保持完好的，可申请七天无理由退货。"]
    feedback = model.prompts[-1][-1]
    assert feedback.role == "user" and feedback.content.startswith(REASK_MARKER) and "数字 9" in feedback.content
    final = by_type(events, "final")[0]
    assert final["text"] == GOOD and final["meta"]["repair"] == {"action": "reask"}


def test_still_wrong_after_the_reask_goes_to_a_person(ask, login, monkeypatch):
    _tiered(monkeypatch, call("search_knowledge", query="七天无理由退货条件"),
            "自签收之日起9天内可申请七天无理由退货[1]。")
    conversation_id, events = ask(login("customer@leon.demo"), "七天无理由退货有什么条件？")
    final = by_type(events, "final")[0]
    assert final["status"] == "handoff" and final["text"].startswith("抱歉，这个问题我没能给出有可靠依据的答复。")
    assert "9天" not in final["text"]
    with new_session() as db:
        assert db.execute(select(Ticket).where(Ticket.conversation_id == conversation_id)).scalars().first()


def test_a_refund_the_model_decided_itself_is_reasked_to_the_tool(ask, login, monkeypatch):
    """Insight 2: under-action is a failure too. The model judged "不能退" without calling request_refund."""
    model = _tiered(monkeypatch, call("get_order", order_id="SO20260918001"),
                    "很抱歉，该订单不符合退款条件，无法为您退款。",
                    call("request_refund", order_id="SO20260918001", reason="不想要了"), "好的。")
    _, events = ask(login("customer@leon.demo"), "SO20260918001 我要退款")
    assert "request_refund" in model.prompts[2][-1].content, "the re-ask tells it the tool decides"
    final = by_type(events, "final")[0]
    assert "已受理" in final["text"] and "不符合" not in final["text"]
