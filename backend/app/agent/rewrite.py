"""Follow-up rewriting for multi-turn conversations.

"七天无理由退货有什么条件？" → "那运费谁出？": searched on its own, the follow-up retrieves the
generic shipping-fee rule (满 99 包邮) instead of who pays return shipping. The runtime
turns a detected follow-up into a standalone question before the agent sees it; the
conversation keeps what the customer actually typed, the trace records both.

Detection is deliberately explicit (leading 那 / 那么 / 还有 / 另外 …, or a trailing 呢):
"short question" alone is a bad signal, because "积分可以怎么抵扣？" is short and complete.
With a real model the rewrite is one cheap fast-tier call; the offline model, or any
failure, falls back to a rule: previous question's topic + the follow-up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm.base import LLMMessage, LLMProvider
from ..textutil import ORDER_ID_RE

_LEADING = ("那么", "那", "还有", "另外", "这个", "那个", "它", "如果", "要是")
_QUESTION_WORDS = re.compile(r"有什么|有哪些|是什么|是多少|怎么|如何|多久|多少|能不能|可不可以|可以吗|吗|呢|[？?。！!]+")
_LEADING_RE = re.compile(r"^(那么|那|还有|另外)[，,、\s]*")

REWRITE_PROMPT = """你是对话改写器。根据对话历史，把用户最后的追问改写成一个独立、完整、不依赖上下文的问题。
只输出改写后的问题，不要回答，不超过 60 字。

对话历史：
{history}

追问：{question}"""


@dataclass
class Rewrite:
    standalone: str
    method: str  # llm | rule


def is_followup(text: str, previous_user_question: str | None) -> bool:
    if not previous_user_question:
        return False
    stripped = text.strip()
    if ORDER_ID_RE.search(stripped):  # an order number makes the question self-contained
        return False
    return stripped.startswith(_LEADING) or stripped.rstrip("？? ").endswith("呢")


def _topic(question: str) -> str:
    """The previous question minus its question words: "七天无理由退货有什么条件？" -> "七天无理由退货条件"."""
    return _QUESTION_WORDS.sub("", question).strip()


def rule_rewrite(text: str, previous_user_question: str) -> str:
    follow = _LEADING_RE.sub("", text.strip())
    return f"{_topic(previous_user_question)}，{follow}"


def rewrite_followup(provider: LLMProvider, model: str, history: list[LLMMessage], text: str,
                     previous_user_question: str) -> tuple[Rewrite, int, int]:
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
    return Rewrite(rule_rewrite(text, previous_user_question), "rule"), 0, 0
