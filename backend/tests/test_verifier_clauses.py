"""N-183: each clause of a claim needs its own basis. The verifier scored a sentence as a whole,
so "退款1-3个工作日到账，所有商品终身保修" passed at 0.6: the true clause carried the invented one,
and the lower bar for sentences with numbers (0.3) made it easier still. Support answers are
written exactly like that, a policy restated and then a promise added."""

import pytest

from app.agent.tools import Evidence
from app.agent.verifier import verify_answer
from app.config import Settings

KB = [
    Evidence(1, "kb", "售后与退款政策",
             "- 自签收之日起7天内，商品保持完好、不影响二次销售的，可申请七天无理由退货。\n"
             "- 退款审核通过后1-3个工作日原路退回。", {"heading": "售后与退款政策"}),
    Evidence(2, "kb", "售后与退款政策",
             "- 签收后15天内发现质量问题，可申请退货或换货，往返运费由Leon优选承担。\n"
             "- 订单支付后48小时内发货，默认使用Atlas快运。", {"heading": "质量问题与发货"}),
]

# A true clause, then an invented one. Each must be blocked, as the invented clause alone is.
PIGGYBACK = [
    "所有商品终身保修，退款1-3个工作日到账。",
    "退款审核通过后1-3个工作日原路退回，所有商品终身保修。",
    "签收7天内可以无理由退货，超过7天也可以联系客服申请，我们一般都会同意。",
    "七天无理由退货需要商品保持完好，赠品可以自己留着。",
    "退款审核通过后1-3个工作日原路退回，另外还会额外赠送积分补偿。",
    "签收后15天内发现质量问题可以退换货，往返运费由Leon优选承担，还能免费上门取件。",
    "订单支付后48小时内发货，偏远地区也包邮。",
    "默认使用Atlas快运，可以免费升级为次日达。",
    "自签收之日起7天内可申请七天无理由退货，拆封使用过的也没关系。",
    "质量问题15天内可以退换，人为损坏也在保修范围内。",
    "退款1-3个工作日原路退回，节假日也会加急处理。",
    "您好，签收后15天内发现质量问题可申请换货，换货期间赠送一个月会员。",
]

# Found by the review (Claude Fable, read-only): promises said with a negation, clauses joined
# without a comma, offers that are promises, and one-word perks.
REVIEW_PIGGYBACK = [
    "退款审核通过后1-3个工作日原路退回，退货没有时间限制。",
    "签收7天内可以无理由退货，不会收取任何手续费。",
    "签收7天内可以无理由退货，未拆封的任何时候都可以退。",
    "订单支付后48小时内发货，偏远地区也不会加收运费。",
    "签收7天内可以无理由退货，赠品无需退回可以自己留着。",
    "质量问题15天内可以退换，不限次数。",
    "退款1-3个工作日到账并且所有商品终身保修。",
    "订单支付后48小时内发货且全国包邮。",
    "退款1-3个工作日到账、所有商品终身保修。",
    "退款审核通过后1-3个工作日原路退回（所有商品终身保修）。",
    "退款审核通过后1-3个工作日原路退回，我们还会给您发一张补偿券。",
    "质量问题15天内可以退换，我们可以帮您免费换新。",
    "签收7天内可以无理由退货，为了方便您我们会额外赠送积分。",
]

# Correct answers with several clauses, written the way the recorded model writes them.
GROUNDED = [
    "签收7天内商品保持完好的，可以申请七天无理由退货。",
    "退款审核通过后，1-3个工作日原路退回。",
    "签收后15天内发现质量问题，可以申请退货或换货，往返运费由Leon优选承担。",
    "订单支付后48小时内发货，默认使用Atlas快运。",
    "根据售后政策，签收7天内可以无理由退货。",
    "唯一例外：签收后15天内发现质量问题，可申请退货或换货。",
    "退款审核通过后1-3个工作日原路退回，无需重复提交。",
    "如需退货，请在签收7天内申请，退款审核通过后1-3个工作日原路退回。",
    "七天无理由退货要求商品保持完好，不影响二次销售。",
    "您的订单已签收超过7天，无法申请无理由退货，但15天内发现质量问题仍可申请换货。",
    # review: hedges and closings next to a grounded clause, a label before a question
    "退款审核通过后1-3个工作日原路退回，具体到账时间以银行为准。",
    "退款审核通过后1-3个工作日原路退回，您耐心等待即可。",
    "退款审核通过后1-3个工作日原路退回，届时请留意到账短信。",
    "温馨提示：退款审核通过后1-3个工作日原路退回，请问还有什么可以帮您？",
    "无法在线申请退款，签收后15天内发现质量问题可以申请换货。",
    # second round: a request or closing that mentions a perk word claims nothing
    "退款审核通过后1-3个工作日原路退回，有问题随时联系我们。",
    "退款审核通过后1-3个工作日原路退回，我们随时为您服务。",
    "签收后15天内发现质量问题可申请换货，请提供订单号以便核实运费。",
    "签收7天内可以无理由退货，建议您保留好赠品和包装。",
    "签收后15天内发现质量问题可申请换货，如需申请运费补偿请提供订单号。",
    "退款审核通过后1-3个工作日原路退回，无需重复提交。",
]


# Second review round: a waiver loosens a condition, it is not a limit; a hedge only skips the
# check when it is the whole clause.
WAIVERS = [
    "签收7天内可以无理由退货，无需原包装。",
    "签收7天内可以无理由退货，不必保留包装也能退。",
    "签收7天内可以无理由退货，不要求提供发票。",
    "退款审核通过后1-3个工作日原路退回，无需审核直接到账。",
    "签收7天内可以无理由退货，超过7天的也可以退以客服答复为准。",
]


@pytest.mark.parametrize("answer", PIGGYBACK + REVIEW_PIGGYBACK + WAIVERS)
def test_an_invented_clause_is_not_carried_by_a_true_one(answer):
    v = verify_answer(answer, KB, "退款政策是什么", Settings())
    assert v.verdict == "blocked", [(s.score, s.weak_clause) for s in v.sentences]
    assert any(s.weak_clause for s in v.sentences)


@pytest.mark.parametrize("answer", GROUNDED)
def test_true_clauses_still_pass(answer):
    v = verify_answer(answer, KB, "退款政策是什么", Settings())
    assert v.verdict == "pass", [(s.score, s.weak_clause) for s in v.sentences]


def test_the_number_bar_covers_only_its_own_clause():
    # Whole sentence 0.6 with numbers (bar 0.3); the invented clause has no number, so it needs 0.5.
    v = verify_answer("所有商品终身保修，退款1-3个工作日到账。", KB, "q", Settings())
    s = v.sentences[0]
    assert s.score >= 0.5 and not s.supported and s.weak_clause == "所有商品终身保修"
    # a clause whose own numbers are checked keeps the lower bar
    assert verify_answer("超过15天的，质量问题也不能退换，7天内可无理由退货。", KB, "q", Settings()).verdict == "pass"


def test_a_limit_clause_is_left_to_the_sentence_score():
    """Known limit, kept on purpose: a clause that leads with "无法 / 不支持 / 无需" is not checked
    alone. The recorded model writes "无需重复提交", "审核前无法承诺结果" next to grounded facts all the
    time; an invented limit costs the customer an answer, an invented promise costs the business
    money. A negation that is really a promise ("不会收取手续费") is checked (REVIEW_PIGGYBACK)."""
    v = verify_answer("签收7天内可以无理由退货，但不支持开发票。", KB, "q", Settings())
    assert v.verdict == "pass"


# Known limit of a word-overlap check: the invented clause reuses the evidence's own words (the KB
# says 往返运费由Leon优选承担 for quality returns). These need LOOPDESK_VERIFIER=llm.
SAME_WORDS = [
    "签收7天内可以无理由退货，我们会为您承担运费。",
    "退款审核通过后1-3个工作日原路退回，免运费。",
    "订单支付后48小时内发货，送运费险。",
]


@pytest.mark.parametrize("answer", SAME_WORDS)
def test_known_limit_an_invented_clause_in_the_evidence_words(answer):
    assert verify_answer(answer, KB, "q", Settings()).verdict == "pass"


def test_the_judge_clears_the_clause_note():
    class Judge:
        def chat(self, *args, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(content='{"supported": [true]}')

    v = verify_answer("所有商品终身保修，退款1-3个工作日到账。", KB, "q", Settings(), judge=Judge())
    assert v.method == "llm" and v.sentences[0].supported and v.sentences[0].weak_clause is None


def test_the_clause_check_reads_all_evidence_not_only_the_cited_chunk():
    # cites [1] but restates [2] in its second clause: the citation rule is the sentence's business
    v = verify_answer("七天无理由退货要求商品完好[1]，质量问题15天内可以换货[1]。", KB, "q", Settings())
    assert v.verdict == "pass"
