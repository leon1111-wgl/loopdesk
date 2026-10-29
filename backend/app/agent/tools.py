"""Tool registry with risk levels and approval policies.

A tool declares up to four things beyond its schema:
- ``risk``: read (no side effects) / write (reversible side effects) / high (money moves);
- ``precheck``: validation that must pass BEFORE a human is asked to approve anything;
- ``approval``: a policy that returns a reason when this specific call needs a human;
- ``executed``: for a tool with an approval policy, its result if it already ran for a given
  approval, else None (app.ops.stale_runs asks after a resume was killed).

Identity for permission checks comes from ``ToolContext`` (the logged-in user), never
from model-generated arguments, so prompt injection cannot widen what a tool may do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings
from ..integrations import OrderGateway, RefundGateway
from ..integrations.local import LocalOrderGateway, LocalRefundGateway
from ..llm.base import Embedder, ToolSpec
from ..models import Tenant, User


class ToolError(Exception):
    """Expected business failure; its message is shown to the model as the tool result."""


class ToolUnavailable(ToolError):
    """A system the tool needs did not answer, before the tool did anything. To a customer it is an
    ordinary tool error; carrying out a reviewer's approval, it is a failure of the resume, which
    puts the approval back to pending (AgentRuntime.resume) instead of spending the decision on a
    refund that was never filed."""


@dataclass
class Evidence:
    ref: int
    kind: str  # kb | tool
    title: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "kind": self.kind, "title": self.title, "text": self.text, "meta": self.meta}


@dataclass
class ToolContext:
    db: Session
    settings: Settings
    tenant: Tenant
    user: User
    embedder: Embedder
    run_id: str
    conversation_id: int | None
    dry_run: bool = False
    # The eval sandbox and demo traffic: tenant tools reach only the synthetic merchant systems.
    sandboxed: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    approval_id: int | None = None  # set when executing a call a human approved
    # The order and payment systems (app/integrations); the demo tables when not given.
    orders: OrderGateway | None = None
    refunds: RefundGateway | None = None
    deadline: float | None = None  # the run's, time.monotonic(): gateway calls make no attempts past it
    customer_text: str = ""  # what the customer wrote this turn, PII-masked (the handoff ticket quotes it)
    reranker: Any = None  # app.rag.rerank.Reranker, when one is configured (W5)
    tools: "ToolRegistry | None" = None  # the tools of this tenant's agent (app/tooling/toolset.py)
    commerce: Any = None  # the Commerce the run uses: routes tenant tools in the sandbox (app/integrations)

    def __post_init__(self) -> None:
        if self.orders is None:
            self.orders = LocalOrderGateway(self.db, self.tenant.id)
        if self.refunds is None:
            self.refunds = LocalRefundGateway(self.db, self.tenant.id)

    @property
    def is_staff(self) -> bool:
        return self.user.role in ("admin", "agent")

    def add_evidence(self, kind: str, title: str, text: str, **meta: Any) -> Evidence:
        item = Evidence(ref=len(self.evidence) + 1, kind=kind, title=title, text=text, meta=meta)
        self.evidence.append(item)
        return item


@dataclass
class ApprovalNeed:
    """Why a call has to wait for a human.

    ``reason`` is for the reviewer and staff: every rule that fired, with its numbers.
    ``customer_note`` is the part a customer may be told, i.e. public policy only. Risk
    controls stay internal: "a fourth refund within 30 days needs review" teaches people
    to make three. ``approver_role`` names who may approve when the tool's policy says so
    (a tenant tool); otherwise the refund rule by amount applies (api/approvals.py).
    """

    reason: str
    customer_note: str = ""
    approver_role: str | None = None


class ApprovalRequired(Exception):
    """Raised by a handler that finds, while holding its locks, a reason for a person to decide
    after all. The runtime turns it into the same approval pause as a policy decision would."""

    def __init__(self, need: ApprovalNeed):
        super().__init__(need.reason)
        self.need = need


Handler = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]
Policy = Callable[[ToolContext, dict[str, Any]], "ApprovalNeed | str | None"]
Executed = Callable[[Session, int], "dict[str, Any] | None"]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk: str = "read"
    roles: tuple[str, ...] = ("admin", "agent", "customer")
    precheck: Handler | None = None
    approval: Policy | None = None
    executed: Executed | None = None
    # The arguments that make a write the same one again later in a run (N-201): a refund on an order
    # already refunded is a repeat whatever its reason says. None compares every argument.
    repeat_on: tuple[str, ...] | None = None
    # Tenant-declared tools (app/tooling): where it comes from, what people call it, how an
    # amount in its arguments is written ("HKD 120.00"), and the offline model's hints.
    source: str = "builtin"  # builtin | http | mcp
    label: str = ""
    amount_text: Callable[[dict[str, Any], str], str] | None = None  # (arguments, tenant currency)
    hints: dict[str, Any] | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters, self.hints)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> ToolDef:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def for_role(self, role: str) -> list[ToolDef]:
        return [t for t in self._tools.values() if role in t.roles]

    def all(self) -> list[ToolDef]:
        return list(self._tools.values())
