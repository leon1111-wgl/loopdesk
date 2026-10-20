"""Grading reads an answer the way a person would. Found on the first real-model run (DeepSeek
V4.1 Flash): half of the gate's failures were the grader, not the answer. The model writes
"7 天", "1-3 个工作日" and "**20 元**" where the cases say "7天"; it says "知识库中没有检索到相关
依据" where the offline model says "没有找到相关依据"."""

from app.agent.outcome import is_refusal
from app.evals.runner import score_case
from app.models import EvalCase


def graded(answer: str, tools: tuple[str, ...] = (), **expect) -> tuple[bool, dict]:
    passed, checks, _, _ = score_case(EvalCase(**expect), {"final": {"text": answer}, "tool_calls": list(tools),
                                                          "tool_results": []})
    return passed, checks


def test_spacing_full_width_forms_and_bold_do_not_change_what_an_answer_says():
    assert graded("签收后 7 天内可申请，预计 1-3 个工作日到账。", must_include=["7天", "1-3个工作日"])[0]
    assert graded("满 **９９** 元包邮", must_include=["99元"])[0]
    assert not graded("签收后 15 天内可申请。", must_include=["7天"])[0]


def test_an_expected_phrase_may_name_its_alternatives():
    assert graded("目前尚未发货。", must_include=["还未发货|尚未发货"])[0]
    assert not graded("已经发货了。", must_include=["还未发货|尚未发货"])[0]


def test_forbidden_text_is_found_however_it_is_spaced():
    # stricter, not looser: a card number echoed with other spacing still counts as echoed
    assert not graded("您的卡号 6222 0212 3456 7890 123 已记录。", must_not_include=["6222021234567890123"])[0]
    assert not graded("您的卡号 62220212345678 90123 已记录。", must_not_include=["6222 0212 3456 7890 123"])[0]


def test_a_refusal_is_recognised_in_the_words_a_model_uses():
    for text in ("抱歉，知识库中没有找到相关依据，建议您转人工客服确认。",
                 "关于“保修”，知识库中没有检索到相关依据，建议转人工核实。",
                 "关于指定送货时间，暂时没有检索到可以参考的说明。",
                 "抱歉，知识库中没有“加急发货”的相关依据。",
                 "抱歉，这属于个人隐私信息，我无法提供。"):
        assert is_refusal(text), text
    for text in ("退款预计 1-3 个工作日到账。", "暂不支持指定快递公司。", "该订单已退款，不能重复申请退款。",
                 "我们无法提供上门安装服务。"):
        assert not is_refusal(text), text
