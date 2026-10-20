"""Spoken confirmation: a money action is read back and runs only after the customer says yes."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..agent.tools import ToolContext, ToolDef, ToolError, ToolRegistry

_PUNCT = re.compile(r"[\s，,。.！!？?~～、]+")
_NO = re.compile(r"不对|不是|错了|取消|不要|算了|不用|别退|搞错|no\b")
_YES = ("确认", "确定", "对", "是", "嗯", "好", "行", "可以", "没错", "没问题", "ok", "yes")


def classify_reply(text: str) -> str | None:
    """"yes", "no", or None when the reply is a new request rather than an answer."""
    t = _PUNCT.sub("", text or "").lower()
    if not t or len(t) > 10:
        return None
    if _NO.search(t):
        return "no"
    return "yes" if t.startswith(_YES) else None


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
