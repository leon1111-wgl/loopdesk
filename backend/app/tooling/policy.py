"""Risk tiers and approval policies of tenant-declared tools.

    risk: read | write | money
    approval: {"mode": "never" | "threshold" | "always",
               "amount_field": "amount", "currency_field": "currency",
               "auto_limits": {"HKD": 200, "CNY": 180},   # threshold: up to this, no person needed
               "approver": "agent" | "admin",              # who decides
               "admin_above": {"HKD": 5000},               # above this only an administrator approves
               "customer_note": "退还年费需要人工核实"}       # what a customer may be told

The rules an administrator cannot switch off: a money tool always has a person in the loop above
some amount (mode never is refused for it), an amount in a currency without a limit goes to a person,
and a read tool never asks for approval (it changes nothing; the agent registry decides whether it
may be used at all).

A money tool is never looser than the tenant's own refund tiers (N-228): its automatic limit in the
tenant's currency is at most ``auto_refund_limit``, above ``approval_admin_threshold`` only an
administrator approves, and limits in other currencies are refused (without an exchange rate they
cannot be compared with the tiers; such an amount always waits for an administrator). The same caps
apply when a call is checked, so a policy stored before them is held to them too.
"""

from __future__ import annotations

import math
from typing import Any

from ..agent.tools import ApprovalNeed, ToolContext

RISKS = ("read", "write", "money")
RUNTIME_RISK = {"read": "read", "write": "write", "money": "high"}  # the runtime's names (agent/tools.py)
MODES = ("never", "threshold", "always")
ROLES = ("agent", "admin")
CURRENCY_SIGN = {"CNY": "¥"}


class PolicyError(ValueError):
    pass


def check_policy(risk: str, approval: dict[str, Any], parameters: dict[str, Any], tenant: Any = None) -> dict[str, Any]:
    """The policy as stored, or PolicyError. ``parameters`` is the tool's (checked) JSON Schema; ``tenant``
    caps a money tool at its refund tiers."""
    if risk not in RISKS:
        raise PolicyError(f"risk 只能是 {' / '.join(RISKS)}")
    approval = dict(approval or {})
    mode = approval.setdefault("mode", "never" if risk == "read" else "always")
    if mode not in MODES:
        raise PolicyError(f"审批方式只能是 {' / '.join(MODES)}")
    if risk == "read" and mode != "never":
        raise PolicyError("只读工具不改变任何东西，不需要审批；是否允许使用由 Agent 清单决定")
    if risk == "money" and mode == "never":
        raise PolicyError("资金类工具必须有人工审批（threshold 或 always）")
    if approval.setdefault("approver", "agent") not in ROLES:
        raise PolicyError("审批人只能是 agent（坐席）或 admin（管理员）")
    props = (parameters or {}).get("properties") or {}
    if mode == "threshold" or approval.get("admin_above"):
        field = approval.setdefault("amount_field", "amount")
        if props.get(field, {}).get("type") not in ("number", "integer"):
            raise PolicyError(f"按金额审批需要一个数字参数 {field}")
        currency_field = approval.get("currency_field")
        if currency_field and props.get(currency_field, {}).get("type") != "string":
            raise PolicyError(f"币种参数 {currency_field} 必须是 string")
    for key in ("auto_limits", "admin_above"):
        limits = approval.get(key) or {}
        if not isinstance(limits, dict) or not all(
                isinstance(c, str) and len(c) == 3 and c.isupper() and _amount(v) is not None and v >= 0
                for c, v in limits.items()):
            raise PolicyError(f"{key} 必须是 {{\"HKD\": 200}} 这样的币种到金额的对应")
    if mode == "threshold" and not approval.get("auto_limits"):
        raise PolicyError("threshold 需要 auto_limits：每个币种自动办理的上限")
    if risk == "money" and tenant is not None:
        _within_tiers(approval, tenant)
    note = approval.get("customer_note", "")
    if not isinstance(note, str) or len(note) > 200:
        raise PolicyError("customer_note 是给顾客看的一句话，最多 200 字")
    return approval


def _within_tiers(approval: dict[str, Any], tenant: Any) -> None:
    currency = tenant.currency or "CNY"
    auto, admin = float(tenant.auto_refund_limit), float(tenant.approval_admin_threshold)
    for key in ("auto_limits", "admin_above"):
        other = sorted(c for c in approval.get(key) or {} if c != currency)
        if other:
            raise PolicyError(f"资金类工具只能按本租户币种 {currency} 设额度（{key} 里有 {'、'.join(other)}）："
                              "没有汇率，其他币种无法和退款分级比较，这类金额一律由管理员审批")
    limit = (approval.get("auto_limits") or {}).get(currency)
    if limit is not None and limit > auto:
        raise PolicyError(f"资金类工具的自动办理额度不能高于本租户的自动退款额度 {amount_text(auto, currency)}"
                          f"（现在是 {amount_text(limit, currency)}）")
    above = approval.setdefault("admin_above", {}).setdefault(currency, admin)
    if above > admin:
        raise PolicyError(f"资金类工具超过 {amount_text(admin, currency)} 必须由管理员审批，和本租户的退款分级一致"
                          f"（现在是 {amount_text(above, currency)}）")


def amount_of(approval: dict[str, Any], args: dict[str, Any], tenant_currency: str) -> tuple[float | None, str]:
    field = approval.get("amount_field", "amount")
    currency_field = approval.get("currency_field")
    currency = str(args.get(currency_field) or tenant_currency).upper() if currency_field else tenant_currency
    return _amount(args.get(field)), currency


def amount_text(amount: float | None, currency: str) -> str:
    if amount is None:
        return ""
    sign = CURRENCY_SIGN.get(currency)
    return f"{sign}{amount:,.2f}" if sign else f"{currency} {amount:,.2f}"


def need_approval(ctx: ToolContext, label: str, approval: dict[str, Any], args: dict[str, Any],
                  money: bool = False) -> ApprovalNeed | None:
    mode = approval.get("mode", "never")
    if mode == "never":
        return None
    tenant_currency = ctx.tenant.currency or "CNY"
    amount, currency = amount_of(approval, args, tenant_currency)
    approver = approval.get("approver", "agent")
    admin_above = (approval.get("admin_above") or {}).get(currency)
    if money:  # the tenant's refund tiers bound every money tool, whatever its stored policy says (N-228)
        tier = float(ctx.tenant.approval_admin_threshold)
        admin_above = None if currency != tenant_currency else min(tier, admin_above if admin_above is not None else tier)
        if currency != tenant_currency or amount is None:
            approver = "admin"
    if amount is not None and admin_above is not None and amount > admin_above:
        approver = "admin"
    note = approval.get("customer_note", "")
    if mode == "always":
        return ApprovalNeed(f"{label}按租户策略每笔都需要人工审批", note, approver)
    limit = (approval.get("auto_limits") or {}).get(currency)
    if money and limit is not None:
        limit = min(limit, float(ctx.tenant.auto_refund_limit))
    if amount is None:
        return ApprovalNeed(f"{label}没有金额，无法按额度自动办理", note, approver)
    if limit is None:
        return ApprovalNeed(f"{currency} 没有设置自动办理额度，需人工审批", note, approver)
    if amount > limit:
        return ApprovalNeed(f"金额 {amount_text(amount, currency)} 超过自动办理额度 {amount_text(limit, currency)}",
                            note, approver)
    return None


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)
