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
from ..textutil import ORDER_ID_RE

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
                     previous: str | Frame) -> tuple[Rewrite, int, int]:
    """Returns the rewrite plus (prompt_tokens, completion_tokens) spent on it."""
    if provider.name != "mock":
        transcript = "\n".join(f"{'用户' if m.role == 'user' else '客服'}：{m.content}" for m in history[-4:])
        try:
            result = provider.chat([LLMMessage(role="user", content=REWRITE_PROMPT.format(history=transcript, question=text))],
                                   [], model, {"task": "rewrite", "temperature": 0})
            standalone = result.content.strip().splitlines()[0][:120] if result.content.strip() else ""
            if standalone:
                return Rewrite(standalone, "llm"), result.usage.prompt_tokens, result.usage.completion_tokens
        except Exception:  # rewriting is an optimisation; never fail the turn because of it
            pass
    return Rewrite(rule_rewrite(text, previous), "rule"), 0, 0
