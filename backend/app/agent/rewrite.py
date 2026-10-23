"""Follow-up rewriting for multi-turn conversations.

"七天无理由退货有什么条件？" → "那运费谁出？": searched on its own, the follow-up retrieves the
generic shipping-fee rule (满 99 包邮) instead of who pays return shipping. The runtime
turns a detected follow-up into a standalone question before the agent sees it; the
conversation keeps what the customer actually typed, the trace records both.

Detection is deliberately explicit (leading 那 / 那么 / 还有 / 另外 …, or a trailing 呢):
"short question" alone is a bad signal, because "积分可以怎么抵扣？" is short and complete.
With a real model the rewrite is one cheap fast-tier call; the offline model, or any
failure, falls back to a rule: previous question's topic + the follow-up.

Chains: the reference is the previous turn's *standalone* question (kept in the user
message's meta), not what was typed, so "那质量问题呢？" after "那运费谁出？" still knows
the topic is returns. An additive opener followed by a complete question ("还有能开发票吗")
starts a new topic and is only stripped of its opener.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm.base import LLMMessage, LLMProvider
from ..textutil import ORDER_ID_RE, REFUND_AMOUNT, REFUND_RE, refund_withdraw_requested

_LEADING = ("那么", "那", "还有", "另外", "这个", "那个", "它", "如果", "要是")
_QUESTION_WORDS = re.compile(r"有什么|有哪些|是什么|是多少|怎么|如何|多久|多少|能不能|可不可以|可以吗|吗|呢|[？?。！!]+")
_LEADING_RE = re.compile(r"^(那么|那|还有|另外)[，,、\s]*")
_ADDITIVE = ("还有", "另外")
_OWN_QUESTION = re.compile(r"吗|谁|啥|怎么|如何|多久|多少|能不能|可不可以|可以|是否|什么|哪|几|要不要|会不会")
_END_PUNCT = "？?！!。 "

REWRITE_PROMPT = """你是对话改写器。根据对话历史，把用户最后的追问改写成一个独立、完整、不依赖上下文的问题。
只输出改写后的问题，不要回答，不超过 60 字。

对话历史：
{history}

追问：{question}

如果追问本身已经是一个完整的新问题（例如“还有能开发票吗”），去掉开头的连接词后原样输出，不要拼接上文。"""


@dataclass
class Rewrite:
    standalone: str
    method: str  # llm | rule


@dataclass
class Frame:
    """What the conversation is about (topic) and what was last asked about it (predicate).

    "七天无理由退货有什么条件？" -> topic and predicate "七天无理由退货条件"; then "那运费谁出？"
    keeps the topic and asks something new (predicate "运费谁出"); then "那质量问题呢？"
    swaps the topic and keeps the question: "质量问题，运费谁出"."""

    topic: str
    predicate: str


def is_followup(text: str, previous_user_question: str | None) -> bool:
    if not previous_user_question:
        return False
    stripped = text.strip()
    if ORDER_ID_RE.search(stripped):  # an order number makes the question self-contained
        return False
    return stripped.startswith(_LEADING) or stripped.rstrip("？? ").endswith("呢")


def _topic(question: str) -> str:
    """A question minus its opener and question words: "七天无理由退货有什么条件？" -> "七天无理由退货条件"."""
    return _QUESTION_WORDS.sub("", _LEADING_RE.sub("", question.strip())).strip()


def frame_of(question: str) -> Frame:
    """The frame a self-contained question opens."""
    topic = _topic(question)
    return Frame(topic, topic)


def _kind(text: str) -> str:
    """new: a complete question after 还有/另外; subject: a bare "那X呢"; predicate: anything else."""
    stripped = text.strip()
    rest = _LEADING_RE.sub("", stripped).rstrip(_END_PUNCT)
    if stripped.startswith(_ADDITIVE) and not rest.endswith("呢") and _OWN_QUESTION.search(rest):
        return "new"
    if rest.endswith("呢") and not _OWN_QUESTION.search(rest[:-1]):
        return "subject"
    return "predicate"


def needs_context(text: str) -> bool:
    """"那运费谁出？" and "还有积分呢？" lean on the previous question; "还有能开发票吗" does not."""
    return _kind(text) != "new"


def _focus(text: str) -> str:
    return _LEADING_RE.sub("", text.strip()).rstrip(_END_PUNCT).removesuffix("呢")


def rule_rewrite(text: str, previous: str | Frame) -> str:
    frame = previous if isinstance(previous, Frame) else frame_of(previous)
    kind = _kind(text)
    if kind == "new":
        return _LEADING_RE.sub("", text.strip())
    if kind == "subject":
        return f"{_focus(text)}，{frame.predicate}"
    return f"{frame.topic}，{_LEADING_RE.sub('', text.strip())}"


def next_frame(previous: str | Frame, text: str, standalone: str) -> Frame:
    """The frame after this follow-up, for the one after it."""
    frame = previous if isinstance(previous, Frame) else frame_of(previous)
    kind = _kind(text)
    if kind == "new":
        return frame_of(standalone)
    if kind == "subject":
        return Frame(_focus(text), frame.predicate)
    return Frame(frame.topic, _topic(text))


def rewrite_followup(provider: LLMProvider, model: str, history: list[LLMMessage], text: str,
                     previous: str | Frame, use_model: bool = True,
                     deadline: float | None = None) -> tuple[Rewrite, int, int]:
    """Returns the rewrite plus (prompt_tokens, completion_tokens) spent on it. ``deadline`` (a
    monotonic time) caps the model call; without the model the rule rewrite is used."""
    if use_model and provider.name != "mock":
        transcript = "\n".join(f"{'用户' if m.role == 'user' else '客服'}：{m.content}" for m in history[-4:])
        try:
            result = provider.chat([LLMMessage(role="user", content=REWRITE_PROMPT.format(history=transcript, question=text))],
                                   [], model, {"task": "rewrite", "temperature": 0,
                                               **({"deadline": deadline} if deadline is not None else {})})
            standalone = result.content.strip().splitlines()[0][:120] if result.content.strip() else ""
            if standalone:
                return Rewrite(standalone, "llm"), result.usage.prompt_tokens, result.usage.completion_tokens
        except Exception:  # rewriting is an optimisation; never fail the turn because of it
            pass
    return Rewrite(rule_rewrite(text, previous), "rule"), 0, 0


# What a turn about the order in hand is made of (N-155): a refund, an amount, taking one back, where the parcel is,
# and fillers. Anything left over ("我的马克杯", "另一单", "一般…要几天", "投诉快递员") may be about another order or a
# policy, so it is not carried: those still list the orders and ask which one, or search the policy.
_CARRY_WORDS = re.compile(
    REFUND_AMOUNT + r"|[¥￥]?\d+(?:\.\d+)?\s*(?:元|块)钱?"
    r"|退款申请|退款|退钱|退货|退掉|退我|退一半|退个?差价|撤回|撤销|收回|取消|作废"
    r"|物流|快递|包裹|到哪了|到哪儿了|到哪|发货了|签收了|送到了|什么时候到|到了"
    r"|嗯|啊|哦|噢|呃|好的|好|行|可以|那|就|吧|呢|吗|了|的|呀|哈|呗|麻烦|请|帮我|给我|我要|我想|想|要|申请|直接|一下"
    r"|也|还是|现在|补偿|赔偿|就行|快点|赶紧|马上|算了|怎么样|查|看看|只|谢谢|谢了|多谢|全额|全部|全款|退了|原卡|原路|差价"
    r"|质量问题|有问题"
    r"|[\s，,。.！!？?~～、…]")
# The previous turn disowned its order ("不对，不是 SO…，是另一单"): nothing to carry.
_DISOWNED = re.compile(r"不是\s*(?:订单\s*)?SO\d|不对[，,\s]*不是|(?:另一|别的|其他|其它)(?:单|订单|笔)", re.IGNORECASE)


_PARCEL = re.compile(r"物流|快递|包裹|到哪|发货了|签收了|送到了|什么时候到|到了")


def carry_order(text: str, previous_user_question: str | None) -> str | None:
    """"SO20261003005 耳机有杂音" → "嗯 退款 100 元": the refund is for that order (N-155).

    A turn that names no order and says nothing but "refund (this much) / take it back / where is it" gets the
    order the previous turn named, so neither the offline model nor a real one searches policy for "100 元".
    Only one order carries: after "SO1 和 SO2" the agent still asks which one. Only a bare turn carries: money
    never moves on an order the customer may not have meant ("我的马克杯想退款" lists the orders, as before).
    The previous question is the standalone one, so the order keeps carrying turn after turn."""
    if not previous_user_question or ORDER_ID_RE.search(text) or _DISOWNED.search(previous_user_question):
        return None
    named = list(dict.fromkeys(m.upper() for m in ORDER_ID_RE.findall(previous_user_question)))
    if len(named) != 1:
        return None
    if not (REFUND_RE.search(text) or refund_withdraw_requested(text) or _PARCEL.search(text)):
        return None
    if _CARRY_WORDS.sub("", text) or text.strip().rstrip(_END_PUNCT).endswith("呢"):
        return None  # "那退款呢" stays a follow-up question, rewritten as before; refund_requested reads it as a question
    return f"{named[0]} {text.strip()}"
