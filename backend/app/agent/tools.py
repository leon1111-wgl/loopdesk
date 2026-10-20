"""Tool registry with risk levels and approval policies.

A tool declares three things beyond its schema:
- ``risk``: read (no side effects) / write (reversible side effects) / high (money moves);
- ``precheck``: validation that must pass BEFORE a human is asked to approve anything;
- ``approval``: a policy that returns a reason when this specific call needs a human.

Identity for permission checks comes from ``ToolContext`` (the logged-in user), never
from model-generated arguments, so prompt injection cannot widen what a tool may do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..config import Settings
from ..llm.base import Embedder, ToolSpec
from ..models import Tenant, User


class ToolError(Exception):
    """Expected business failure; its message is shown to the model as the tool result."""


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
    evidence: list[Evidence] = field(default_factory=list)
    approval_id: int | None = None  # set when executing a call a human approved

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
    to make three.
    """

    reason: str
    customer_note: str = ""


class ApprovalRequired(Exception):
    """Raised by a handler that finds, while holding its locks, a reason for a person to decide
    after all. The runtime turns it into the same approval pause as a policy decision would."""

    def __init__(self, need: ApprovalNeed):
        super().__init__(need.reason)
        self.need = need


Handler = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]
Policy = Callable[[ToolContext, dict[str, Any]], "ApprovalNeed | str | None"]


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

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


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
