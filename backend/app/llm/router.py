"""Model routing: send easy questions to the fast tier, risky or complex ones to the
strong tier, and fall back to the other tier when a call fails."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Settings
from ..textutil import ORDER_ID_RE

# Escalation language is always high stakes; a refund is high stakes only when it is an
# action on a concrete order ("SO… 我要退款"), not a policy question ("退款多久到账").
_ESCALATION = re.compile(r"赔偿|索赔|投诉|起诉|律师|法律|报警|曝光|12315")
_MONEY_ACTION = re.compile(r"退款|退钱|退货")
_INTENTS = [
    re.compile(r"退款|退货"),
    re.compile(r"物流|快递|发货"),
    re.compile(r"发票"),
    re.compile(r"积分|会员"),
    re.compile(r"报修|故障|坏了"),
]


@dataclass
class RouteDecision:
    tier: str
    model: str
    reasons: list[str]


class ModelRouter:
    def __init__(self, settings: Settings):
        self.fast = settings.llm_model_fast
        self.strong = settings.llm_model_strong

    def route(self, text: str, history_turns: int = 0) -> RouteDecision:
        reasons: list[str] = []
        if len(text) > 80:
            reasons.append("问题较长")
        if sum(1 for p in _INTENTS if p.search(text)) >= 2:
            reasons.append("多意图")
        if _ESCALATION.search(text) or (_MONEY_ACTION.search(text) and ORDER_ID_RE.search(text)):
            reasons.append("高风险意图")
        if history_turns >= 4:
            reasons.append("多轮上下文")
        if reasons:
            return RouteDecision("strong", self.strong, reasons)
        return RouteDecision("fast", self.fast, ["简单问题"])

    def candidates(self, decision: RouteDecision) -> list[str]:
        """Primary model first, then the other tier as fallback."""
        other = self.fast if decision.model == self.strong else self.strong
        return [decision.model] if other == decision.model else [decision.model, other]
