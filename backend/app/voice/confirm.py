"""Spoken confirmation: a money action is read back and runs only after the customer says yes."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..agent.tools import ToolContext, ToolDef, ToolError, ToolRegistry

_PUNCT = re.compile(r"[\s，,。.！!？?~～、…]+")
_NO = re.compile(r"不对|不是|错了|取消|不要|算了|不用|别退|搞错|不确认|不行|no\b")
# A confirmation is a closed set of whole answers, clause by clause ("对的，退吧"). Anything else that
# merely starts like one ("好的帮我查下物流", "可以退另一个订单吗") is a new request, not a yes: a false
# yes moves money, a false "not an answer" only costs a repeated question (review, P0).
_YES = {"确认", "确定", "对", "对的", "是", "是的", "没错", "好", "好的", "可以", "行", "嗯", "没问题", "ok", "okay",
        "yes", "确认退款", "确定退款", "退吧", "就退吧", "可以退", "确认无误", "没错就是这个", "是这个", "就这个"}
_NOT_AN_ANSWER = re.compile(r"[吗呢]|另|换|查|问|其他|还|再|想|一下|多少|什么|怎么|哪")
_PARTICLES = re.compile(r"[吧啊呀了哦嘛]+$")


def _clause_is_yes(clause: str) -> bool:
    clause = re.sub(r"(.)\1+$", r"\1", clause)  # 嗯嗯 / 对对对
    return clause in _YES or _PARTICLES.sub("", clause) in _YES


def classify_reply(text: str) -> str | None:
    """"yes", "no", or None when the reply is not an answer to the read-back."""
    raw = (text or "").strip().lower()
    t = _PUNCT.sub("", raw)
    if not t or len(t) > 12:
        return None
    if _NO.search(t):
        return "no"
    if _NOT_AN_ANSWER.search(t):
        return None
    clauses = [c for c in _PUNCT.split(raw) if c]
    return "yes" if clauses and all(_clause_is_yes(c) for c in clauses) else None


def readback_refund(order_id: str, amount: float) -> str:
    value = f"{amount:g}" if amount != int(amount) else str(int(amount))
    return (f"为了您的资金安全，跟您确认一下：订单号{order_id}，退款金额{value}元。"
            "确认请说“确认”，不对请说“取消”。")


def readback_entities(order_id: str | None, amount: str | None) -> str:
    parts = ([f"订单号是{order_id}"] if order_id else []) + ([f"金额是{amount}元"] if amount else [])
    return f"我跟您确认一下，{'，'.join(parts)}，对吗？"


# ---------------------------------------------------------------- the gate in front of money tools
VOICE_CONFIRM_ERROR = ("语音渠道：涉及资金的操作需要顾客先口头确认订单号和金额。系统会向顾客复述并等待确认，"
                       "请不要说操作已经提交。")


@dataclass
class Pending:
    tool: str
    order_id: str
    amount: float
    utterance: str  # what the customer asked, re-submitted once they confirm
    readback: object = None  # the spoken read-back (session.Sentence): a yes counts only once it was heard


def _key(tool: str, order_id: object, amount: object) -> tuple[str, str, float]:
    try:
        value = round(float(amount or 0), 2)
    except (TypeError, ValueError):
        value = -1.0
    return tool, str(order_id or "").strip().upper(), value


class Confirmations:
    """Per voice session. A confirmation allows exactly one call with exactly these arguments:
    a second refund in the same turn, or one for a different amount, is read back again."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: Pending | None = None
        self.allowed: list[tuple[str, str, float]] = []

    def request(self, tool: str, args: dict, utterance: str) -> None:
        with self.lock:
            self.pending = Pending(tool, str(args.get("order_id") or "").upper(), float(args.get("amount") or 0),
                                   utterance)

    def allow(self, tool: str, order_id: str, amount: float) -> None:
        with self.lock:
            self.allowed.append(_key(tool, order_id, amount))

    def new_turn(self) -> None:
        """Each utterance starts clean: a confirmation the run did not use does not carry over."""
        with self.lock:
            self.allowed.clear()

    def take(self, tool: str, args: dict) -> bool:
        key = _key(tool, args.get("order_id"), args.get("amount"))
        with self.lock:
            if key in self.allowed:
                self.allowed.remove(key)
                return True
            return False

    def accept(self) -> Pending | None:
        with self.lock:
            pending, self.pending = self.pending, None
            if pending:
                self.allowed.append(_key(pending.tool, pending.order_id, pending.amount))
            return pending

    def decline(self) -> None:
        with self.lock:
            self.pending = None


def gate_registry(base: ToolRegistry, confirmations: Confirmations, utterance: Callable[[], str]) -> ToolRegistry:
    """The same tools, with every money-moving tool (risk "high") behind a spoken confirmation.
    The check sits in the precheck, after the tool's own validation and normalisation, so what is
    read back is exactly what would run (an omitted amount already made explicit), and before the
    approval policy: the customer confirms, then a reviewer decides if the policy asks for one.
    Carrying out an approved call (approval_id set) is not asked again."""
    registry = ToolRegistry()
    for tool in base.all():
        if tool.risk != "high":
            registry.register(tool)
            continue

        def precheck(ctx: ToolContext, args: dict, tool: ToolDef = tool) -> dict | None:
            extra = tool.precheck(ctx, args) if tool.precheck else None
            normalized = {**args, **(extra or {})}
            if ctx.approval_id is None and not confirmations.take(tool.name, normalized):
                confirmations.request(tool.name, normalized, utterance())
                raise ToolError(VOICE_CONFIRM_ERROR)
            return extra

        registry.register(replace(tool, precheck=precheck))
    return registry
