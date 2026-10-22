"""Status sentences rendered from tool fields, and the model's own status sentences checked against them.

Grounded is not the same as correct for this customer. On the first real-model run an answer said "退款将在
1-3 个工作日原路退回" after the refund had been rejected: the sentence is in the refund policy, so the
evidence check passed it. What a customer is told about their own case (a refund filed, waiting for
review, rejected, being confirmed, paid out, failed; a ticket and its SLA) is therefore rendered here from
the fields the tool returned, the way Parlant's canned responses work, and the model writes only the
framing around it. A template whose fields are missing is not used: a sentence with a blank in it would
be worse than the model's. Whatever the model says about the case itself is compared with the tool state:

    case_state(evidence, customer_text)  what the tools in this run say: refund, ticket, handoff
    claimed_states(sentence)             what a sentence says about them
    contradiction(sentence, state)       why the two disagree, or None
    render_status(state)                 the status sentences, from the tool fields

The claims are matched only when a sentence states a status. A condition ("审核通过后会原路退回") is
policy, not a status; a sentence about refunds in general counts only where it would mislead: after a
refund was rejected, failed or is unconfirmed, any payout wording does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..textutil import CITE_RE, ORDER_ID_RE, normalize_width, refund_requested
from .tools import Evidence

PRIORITY_TEXT = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}


@dataclass(frozen=True)
class Template:
    text: str
    required: tuple[str, ...]

    def missing(self, fields: dict[str, Any]) -> list[str]:
        return [k for k in self.required if fields.get(k) in (None, "")]

    def render(self, fields: dict[str, Any]) -> str | None:
        if self.missing(fields):
            return None
        shown = {k: _shown(k, v) for k, v in fields.items()}
        return self.text.format(**shown)


def _shown(key: str, value: Any) -> str:
    if key == "amount":
        return f"{float(value):.2f}"
    if key == "priority":
        return PRIORITY_TEXT.get(str(value), str(value))
    return str(value)


# One template per state. The wording matches what the tools themselves report (business_tools), so a
# customer reads the same sentence in the chat, on a staff screen and in the voice channel.
TEMPLATES: dict[str, Template] = {
    "refund.filed": Template("退款单 #{refund_id} 已受理，退款金额 ¥{amount} 将按原支付方式退回。", ("refund_id", "amount")),
    "refund.drill": Template("（演练）退款申请已受理，退款金额 ¥{amount} 将按原支付方式退回。", ("amount",)),
    "refund.paid": Template("退款单 #{refund_id} 已退款成功，¥{amount} 已按原支付方式退回。", ("refund_id", "amount")),
    "refund.confirming": Template("退款申请已提交（退款单 #{refund_id}），支付系统暂未返回结果，我们会继续核实，请勿重复申请；"
                                  "处理结果以订单状态为准。", ("refund_id",)),
    "refund.failed": Template("退款申请未能提交：{reason}。", ("reason",)),
    "refund.rejected": Template("退款申请未通过人工审核。审核意见：{comment}。", ("comment",)),
    "refund.rejected_bare": Template("退款申请未通过人工审核。", ()),
    "ticket.created": Template("已为您创建工单 #{ticket_id}（优先级：{priority}），技术支持将在 {sla_hours} 小时内联系您。",
                               ("ticket_id", "priority", "sla_hours")),
    "ticket.drill": Template("（演练）已为您创建工单（优先级：{priority}），技术支持将在 {sla_hours} 小时内联系您。",
                             ("priority", "sla_hours")),
}

# ---------------------------------------------------------------- the tool state
# Which claims each refund state allows. "submitted" (已提交/已申请) is true of every state in which a
# request reached the tool; "will_pay" (已受理, 将退回, N 个工作日到账) only once the payment system accepted.
ALLOWED: dict[str, set[str]] = {
    "filed": {"submitted", "will_pay"},
    "paid": {"submitted", "will_pay", "paid"},
    "confirming": {"submitted", "confirming"},
    "awaiting_review": {"submitted", "awaiting_review"},
    "rejected": {"submitted", "rejected", "not_refundable"},
    "failed": {"failed", "not_refundable", "rejected"},
    "refunded_order": {"paid", "not_refundable"},  # the order system shows the order as refunded
    "refunding_order": {"submitted", "will_pay", "not_refundable"},
    "not_refundable": {"not_refundable"},
    "withdrawn": {"withdrawn", "not_refundable"},  # the customer took the application back (N-127)
    "refundable": set(),  # an order the tool would accept: nothing was filed, nothing was decided
    "none": set(),
}
# Where money is not coming (or not known to be), any payout wording misleads, policy or not.
NO_PAYOUT = {"rejected", "failed", "confirming", "withdrawn"}


@dataclass
class CaseState:
    refund: str = "none"
    refund_source: str = ""  # action (request_refund in this run) | withdrawal | ledger (get_order's refunds) | order | ""
    refund_fields: dict[str, Any] = field(default_factory=dict)
    ticket: str = "none"  # created | failed | none
    ticket_fields: dict[str, Any] = field(default_factory=dict)
    handoff: bool = False
    refund_asked: bool = False  # the customer asked for a refund this turn
    quoted: list[str] = field(default_factory=list)  # tool wording the model may quote verbatim


@dataclass
class Rendered:
    refund: str | None = None
    ticket: str | None = None
    unusable: dict[str, list[str]] = field(default_factory=dict)

    def for_domain(self, domain: str) -> str | None:
        return getattr(self, domain)


def _output(item: Evidence) -> dict[str, Any]:
    try:
        value = json.loads(item.text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


_LEDGER_STATE = {"processing": "filed", "succeeded": "paid", "pending": "confirming", "failed": "failed"}


def _refund_action(output: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if output.get("error"):
        return "failed", {"reason": str(output["error"]).rstrip("。")}
    if output.get("status") == "rejected":
        comment = str(output.get("comment") or "").strip()
        return "rejected", {"comment": "" if comment in ("", "无") else comment.rstrip("。")}
    fields = {"refund_id": output.get("refund_id"), "amount": output.get("amount")}
    refund_status = output.get("refund_status")
    if refund_status in ("succeeded", "pending", "failed"):
        return _LEDGER_STATE[refund_status], fields
    return "filed", fields


def case_state(evidence: list[Evidence], customer_text: str) -> CaseState:
    """What the tools in this run say about the customer's case. The last refund call wins over the
    ledger, which wins over the order's refundable flag."""
    state = CaseState(refund_asked=refund_requested(customer_text))
    for item in evidence:
        if item.kind != "tool":
            continue
        output = _output(item)
        for key in ("message", "error", "comment", "refund_note", "summary", "status_text"):
            if output.get(key):
                state.quoted.append(str(output[key]))
        if item.title == "request_refund":
            state.refund, state.refund_fields = _refund_action(output)
            state.refund_source = "action"
        elif item.title == "withdraw_refund_request" and output.get("withdrawn"):
            state.refund, state.refund_source = "withdrawn", "withdrawal"
        elif (item.title == "get_order" and state.refund_source not in ("action", "withdrawal")
              and not output.get("error")):
            waiting, refunds = output.get("refund_awaiting_approval"), output.get("refunds") or []
            if waiting:
                state.refund, state.refund_source = "awaiting_review", "ledger"
            elif refunds:
                state.refund, state.refund_source = _LEDGER_STATE.get(refunds[0].get("status"), "filed"), "ledger"
            elif output.get("status") == "refunded":
                state.refund, state.refund_source = "refunded_order", "order"
            elif output.get("status") == "refunding":
                state.refund, state.refund_source = "refunding_order", "order"
            elif "refundable" in output:
                state.refund = "refundable" if output["refundable"] else "not_refundable"
                state.refund_source = "order"
        elif item.title == "create_ticket":
            if output.get("error"):
                state.ticket, state.ticket_fields = "failed", {"reason": str(output["error"])}
            else:
                state.ticket = "created"
                state.ticket_fields = {k: output.get(k) for k in ("ticket_id", "priority", "sla_hours")}
        elif item.title == "handoff_to_human" and not output.get("error"):
            state.handoff = True
    return state


def render_status(state: CaseState, dry_run: bool = False) -> Rendered:
    """The status sentences for the actions this run took, rendered from the tools' fields."""
    rendered = Rendered()
    if state.refund_source == "action":
        key = {"filed": "refund.drill" if dry_run else "refund.filed", "paid": "refund.paid",
               "confirming": "refund.confirming", "failed": "refund.failed"}.get(state.refund)
        if state.refund == "rejected":
            key = "refund.rejected" if state.refund_fields.get("comment") else "refund.rejected_bare"
        _put(rendered, "refund", TEMPLATES[key], state.refund_fields)
    if state.ticket == "created":
        fields = state.ticket_fields
        _put(rendered, "ticket", TEMPLATES["ticket.drill" if dry_run and fields.get("ticket_id") is None
                                            else "ticket.created"], fields)
    return rendered


def _put(rendered: Rendered, domain: str, template: Template, fields: dict[str, Any]) -> None:
    text = template.render(fields)
    if text is None:
        rendered.unusable[domain] = template.missing(fields)
    else:
        setattr(rendered, domain, text)


# ---------------------------------------------------------------- what a sentence claims
_NEG = r"(?:不会|不能|无法|没有|未能?|不|别|并非|并不)"
# A condition or a rule is policy, not a status: "审核通过后会原路退回", "如果申请退款，款项会…".
_CONDITIONAL = re.compile(r"^(?:如果|若|如|假如|要是|一旦|只要)|(?:通过|批准|同意|审核|签收|确认|申请(?:退款)?|收到退货)(?:之)?后"
                          r"|的话|一般|通常|正常情况下")
_REFUND_PATTERNS: dict[str, re.Pattern] = {
    "paid": re.compile(r"退款成功|已(?:经)?(?:成功)?(?:到账|退款|退回|原路退回|退还|返还)|(?:钱|款项?|退款)(?:已经|已)(?:退|到|打|返)"
                       r"|已按原支付方式退回"),
    "will_pay": re.compile(r"已受理|(?:将|会|预计|即将|很快)[^。，；,]{0,12}(?:退回|到账|退还|返还|退到|原路返回)"
                           r"|\d+\s*[-~～至到]\s*\d+\s*个?工作日[^。，；]{0,4}到账|个?工作日内?(?:到账|退回)"),
    "submitted": re.compile(r"已(?:经)?(?:为您|帮您|给您)?(?:提交|申请|发起|办理)(?:了)?(?:退款|退货|申请)?"
                            r"|(?:退款|退货)?申请已(?:经)?(?:提交|发起|受理)|为您(?:提交|申请|发起)了"),
    "awaiting_review": re.compile(r"人工审核|等待审核|审核中|待审核|提交审核|正在审核|进入审核"),
    "rejected": re.compile(r"未通过|没有通过|没通过|不通过|驳回|被拒|拒绝了"),
    "confirming": re.compile(r"暂未返回|暂时没有返回|核实中|正在核实|继续核实|以订单状态为准|尚未确认|还未确认"),
    "failed": re.compile(r"未能(?:完成|提交|受理)|没有受理|退款失败|提交失败|没能提交"),
    # a refund word or 申请 must follow: "已为您取消自动续费 / 订单" is not about a refund application
    "withdrawn": re.compile(r"已(?:经)?(?:为您|帮您)?(?:撤回|撤销|取消)(?:了)?(?:这笔|您的)?(?:(?:退款|退货)(?:申请)?|申请)"
                            r"|(?:退款|退货)?申请(?:已经|已|被)(?:撤回|撤销|取消)"),
    "not_refundable": re.compile(rf"{_NEG}(?:为您)?(?:申请|办理|发起|支持)?(?:退款|退货)|不符合(?:退款|退货)条件|不在(?:退款|售后)范围"
                                 r"|不能重复申请"),
}
_TICKET_CLAIM = re.compile(r"已(?:经)?(?:为您|帮您)?(?:创建|提交|登记|生成)(?:了)?(?:售后|维修|技术)?工单|工单已(?:经)?(?:创建|提交|生成|登记)")
_HANDOFF_CLAIM = re.compile(r"已(?:经)?(?:为您|帮您)?(?:转接|转给|转至|转到|转)(?:了)?人工|人工客服(?:已|正在)(?:接入|为您服务)")
# A sentence about this customer's refund rather than refunds in general.
_PERSONAL = re.compile(r"您的|为您|帮您|给您|这笔|该订单|此订单|本次|这次|退款单|#\d|您这|您申请的|您提交的")


@dataclass
class Claims:
    refund: set[str] = field(default_factory=set)
    personal: bool = False
    ticket: bool = False
    handoff: bool = False

    def any(self) -> bool:
        return bool(self.refund or self.ticket or self.handoff)


def _without(text: str, quoted: list[str]) -> str:
    """The sentence minus what it quotes: a tool's own message or reason, and anything in “…”."""
    for piece in sorted(quoted, key=len, reverse=True):
        for part in [piece, *re.split(r"[。；;]", piece)]:
            part = part.strip().rstrip("。")
            if len(part) >= 4:
                text = text.replace(part, "")
    return re.sub(r"“[^”]*”|\"[^\"]*\"", "", text)


def _negated(text: str, start: int) -> bool:
    return bool(re.search(_NEG + r"[^，。,；]{0,2}$", text[max(0, start - 4):start]))


def claimed_states(sentence: str, quoted: list[str] | None = None) -> Claims:
    text = _without(normalize_width(sentence), quoted or [])
    claims = Claims(personal=bool(_PERSONAL.search(text) or ORDER_ID_RE.search(text)))
    conditional = bool(_CONDITIONAL.search(text))
    for state, pattern in _REFUND_PATTERNS.items():
        for match in pattern.finditer(text):
            if state in ("paid", "will_pay", "submitted", "awaiting_review", "withdrawn") and _negated(text, match.start()):
                continue
            claims.refund.add(state)
            break
    if "rejected" in claims.refund:  # "审核没有通过" is a rejection, not a pending review
        claims.refund.discard("awaiting_review")
        if "通过" in text:
            claims.refund.discard("not_refundable")
    if conditional:  # what would happen, not what did
        claims.refund -= {"paid", "will_pay", "submitted", "awaiting_review", "withdrawn"}
    claims.ticket = bool(_TICKET_CLAIM.search(text))
    claims.handoff = bool(_HANDOFF_CLAIM.search(text))
    return claims


def contradiction(sentence: str, state: CaseState) -> str | None:
    """Why this sentence disagrees with the tools, or None: "request_refund" (says something about the refund
    the refund tools contradict), "under_action" (decides a refund the customer asked for without the tool),
    "ticket" / "handoff" (says one was made when no tool made it)."""
    claims = claimed_states(sentence, state.quoted)
    if claims.ticket and state.ticket != "created":
        return "ticket"
    if claims.handoff and not state.handoff:
        return "handoff"
    if not claims.refund:
        return None
    allowed = ALLOWED.get(state.refund, set())
    if state.refund in ("refundable", "none"):
        if "not_refundable" in claims.refund and state.refund_asked:
            return "under_action"  # the tool decides whether a requested refund is possible, not the model
        if claims.personal and claims.refund & {"paid", "will_pay", "submitted", "awaiting_review", "rejected", "withdrawn"}:
            return "request_refund"  # says something happened to a refund no tool touched
        return None
    if claims.personal or state.refund in NO_PAYOUT:
        if claims.refund - allowed - ({"not_refundable"} if state.refund in NO_PAYOUT else set()):
            return "request_refund"
    return None


def status_domains(claims: Claims, state: CaseState, cited: bool = False) -> set[str]:
    """The rendered statuses this sentence restates (or misstates), which its template replaces. After this
    run's refund call, a refund status without a policy citation is about this refund; one citing the policy
    counts when it is personal, or wherever money is not coming."""
    domains = set()
    if claims.refund and state.refund_source == "action" and (
            claims.personal or not cited or state.refund in NO_PAYOUT
            or claims.refund & {"rejected", "failed", "confirming"}):
        domains.add("refund")
    if claims.ticket and state.ticket == "created":
        domains.add("ticket")
    return domains


_PIECE_RE = re.compile(r"[^。！？!?；;\n]*(?:[。！？!?；;]+[”」』）)]*|\n+|$)")


def pieces(text: str) -> list[str]:
    """The text cut into sentences that join back into it exactly (line breaks stay with the sentence
    before them), so a sentence can be replaced without losing a list's layout."""
    return [m.group(0) for m in _PIECE_RE.finditer(text) if m.group(0)]


def _core(text: str) -> str:
    return re.sub(r"[\s。！？!?；;，,：:、]+", "", CITE_RE.sub("", normalize_width(text)))


@dataclass
class Composed:
    text: str
    rendered: list[str] = field(default_factory=list)  # domains whose template is in the answer
    replaced: list[str] = field(default_factory=list)   # model sentences a template stands in for
    blocked: list[str] = field(default_factory=list)    # of those, the ones that contradicted the tools
    unusable: dict[str, list[str]] = field(default_factory=dict)
    templates: list[str] = field(default_factory=list)  # the rendered sentences, as they appear

    def meta(self) -> dict[str, Any] | None:
        if not (self.rendered or self.unusable):
            return None
        return {"rendered": self.rendered, "replaced": self.replaced, "blocked": self.blocked,
                "unusable": self.unusable}


def compose(answer: str, state: CaseState, dry_run: bool = False) -> Composed:
    """The answer with every status sentence for an action this run took replaced by its template: the
    template goes where the model first stated that status, or first when it did not. A sentence that
    restates the template or the tool's own message is replaced too, so the status is said once."""
    rendered = render_status(state, dry_run)
    out = Composed(answer, unusable=dict(rendered.unusable))
    templates = {d: rendered.for_domain(d) for d in ("refund", "ticket") if rendered.for_domain(d)}
    if not templates:
        return out
    restated = [_core(t) for t in [*templates.values(), *state.quoted] if _core(t)]
    kept: list[str] = []
    placed: set[str] = set()
    for piece in pieces(answer):
        sentence = piece.strip()
        core = _core(sentence)
        if not core:
            kept.append(piece)
            continue
        claims = claimed_states(sentence, state.quoted)
        domains = status_domains(claims, state, cited=bool(CITE_RE.search(sentence))) & set(templates)
        if not domains and len(core) >= 4 and any(core in r for r in restated):
            domains = {"refund" if "refund" in templates and "工单" not in core else next(iter(templates))}
        if not domains:
            kept.append(piece)
            continue
        out.replaced.append(sentence)
        if contradiction(sentence, state):
            out.blocked.append(sentence)
        tail = piece[len(piece.rstrip()):]
        for domain in sorted(domains - placed):
            kept.append(templates[domain] + tail)
            placed.add(domain)
    first = [templates[d] for d in ("refund", "ticket") if d in templates and d not in placed]
    out.text = "".join(first + kept).strip()
    out.rendered = sorted(templates)
    out.templates = list(templates.values())
    return out
