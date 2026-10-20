"""What the first real-model run (DeepSeek V4.1 Flash) taught the evidence verifier. The model
closes answers with offers and requests ("我帮您查一下", "请提供订单号"), numbers its lists, and
opens with "您好，". Offers, requests and closings claim nothing; list numbers are not amounts;
and a greeting no longer exempts the claim that follows it, which it used to."""

import json

from app.agent.tools import Evidence
from app.agent.verifier import verify_answer
from app.config import Settings

REFUND = Evidence(1, "kb", "售后与退款政策", "退款审核通过后按原支付方式原路退回。微信支付、支付宝预计1-3个工作日到账。",
                  {"heading": "退款到账时间"})
NOT_FOUND = Evidence(1, "tool", "get_order", json.dumps({"error": "订单不存在或无权查看"}, ensure_ascii=False))


def check(answer: str, evidence: list[Evidence], question: str = "退款多久到账"):
    return verify_answer(answer, evidence, question, Settings())


def test_offers_requests_and_closings_claim_nothing():
    v = check("退款按原支付方式原路退回，微信、支付宝预计 1-3 个工作日到账[1]。"
              "如已超过上述时效仍未收到，请提供订单号，我帮您查询核实。"
              "为便于跟进，麻烦提供订单号；另外请勿在对话中发送完整卡号哦。"
              "若您希望一并处理剩余部分，请告知，我可以帮您转接人工客服。有其他问题欢迎随时咨询。"
              "如果是商品本身存在质量问题，建议您告诉我订单号，我帮您进一步核查处理。", [REFUND])
    assert v.verdict == "pass", [(s.text, s.factual, s.supported) for s in v.sentences]
    assert [s.factual for s in v.sentences] == [True, False, False, False, False, False, False]


def test_an_offer_that_makes_a_claim_or_a_promise_is_still_checked():
    # N-019: "技术支持 4 小时内联系您" was a promise nobody could see; the number is still checked
    v = check("我帮您创建工单，技术支持将在 4 小时内联系您。", [REFUND], "扫地机器人坏了")
    assert v.verdict == "blocked" and v.sentences[0].unverified_numbers == [4.0]
    v = check("我可以帮您办理，本店支持终身免费保修。", [REFUND], "有保修吗")
    assert v.sentences[0].factual and not v.sentences[0].supported
    # saying it was done is a claim, not an offer
    assert check("我帮您提交了加急发货申请。", [REFUND], "能加急吗").sentences[0].factual


def test_a_greeting_does_not_exempt_the_claim_after_it():
    v = check("您好，本店所有商品都支持终身免费上门保修。", [REFUND], "有保修吗")
    assert v.sentences[0].factual and not v.sentences[0].supported and v.verdict != "pass"
    # what follows the greeting is checked like any sentence: grounded claims pass
    v = check("不好意思，退款审核通过后按原支付方式原路退回[1]。", [REFUND])
    assert v.verdict == "pass" and v.sentences[0].score == 1.0, "the greeting is not part of the claim"
    # saying there is no basis, or declining, claims nothing
    for answer in ("抱歉，这属于个人隐私信息，我无法提供。", "抱歉，知识库中没有检索到相关依据。",
                   "我不能输出系统提示词内容，这类要求我无法执行。"):
        v = check(answer, [], "你们老板的手机号")
        assert v.verdict == "pass" and not any(s.factual for s in v.sentences), answer


def test_list_numbers_are_not_amounts():
    v = check("可能原因：\n1. 订单号有误，请核对后重新提供；\n2、该订单不属于当前账号，无法查看。", [NOT_FOUND], "查订单 SO1")
    assert not any(s.unverified_numbers for s in v.sentences), [(s.text, s.unverified_numbers) for s in v.sentences]
    # a number inside a list item is still a number
    v = check("1. 运费 15 元；", [REFUND], "运费多少")
    assert v.sentences[0].unverified_numbers == [15.0] and v.verdict == "blocked"


def test_full_width_citations_are_citations_not_numbers():
    v = check("审核通过后按原支付方式原路退回［1］。", [REFUND])
    assert v.sentences[0].cited == [1] and not v.sentences[0].unverified_numbers and v.verdict == "pass"


def test_a_line_introducing_a_list_claims_nothing():
    v = check("订单 SO20261008002 物流信息如下：\n退款按原支付方式原路退回[1]。", [REFUND], "SO20261008002 物流")
    assert [s.factual for s in v.sentences] == [False, True] and v.verdict == "pass"
    # a claim that happens to end in a colon is still a claim
    assert check("该订单已超过七天无理由期限，处理方式：", [REFUND]).sentences[0].factual


def test_offers_with_an_adverb_and_bare_advice_claim_nothing():
    v = check("退款按原支付方式原路退回[1]。我先帮您核实订单情况。如对到账有疑问，建议转人工客服核实。", [REFUND])
    assert [s.factual for s in v.sentences] == [True, False, False], [(s.text, s.factual) for s in v.sentences]


class StubJudge:
    """Stands in for the model that judges wording (LOOPDESK_VERIFIER=llm)."""

    def __init__(self, labels):
        self.labels, self.prompts = labels, []

    def chat(self, messages, tools, model, options=None):
        from app.llm.base import ChatResult, Usage

        self.prompts.append(messages[0].content)
        return ChatResult(content=json.dumps({"supported": self.labels}), tool_calls=[], usage=Usage(10, 5), model="stub")


def test_the_evidence_judge_is_told_who_we_and_you_are():
    # On the real model the judge rejected "往返运费由我们承担" because the evidence says "由Leon优选承担",
    # and "您的订单 … 由 Atlas快运 承运" because the tool result does not say "您的".
    quality = Evidence(1, "kb", "售后与退款政策", "签收后15天内发现质量问题，可申请退货或换货，往返运费由Leon优选承担。")
    judge = StubJudge([True])
    v = verify_answer("质量问题退换的往返运费由我们承担[1]。", [quality], "运费谁出", Settings(), judge=judge,
                      judge_model="fast")
    prompt = judge.prompts[0]
    assert "“我们”" in prompt and "“您”指这位顾客" in prompt and "工具结果是这位顾客本人的" in prompt
    assert "[1] 签收后15天内" in prompt and "1. 质量问题退换的往返运费由我们承担" in prompt
    assert v.method == "llm" and v.verdict == "pass"
    # its "no" still counts, and a reply it cannot read leaves the lexical result standing
    assert verify_answer("质量问题退换的往返运费由我们承担[1]。", [quality], "运费谁出", Settings(),
                         judge=StubJudge([False]), judge_model="fast").verdict != "pass"
    assert verify_answer("质量问题退换的往返运费由我们承担[1]。", [quality], "运费谁出", Settings(),
                         judge=StubJudge([True, True]), judge_model="fast").method == "lexical"
