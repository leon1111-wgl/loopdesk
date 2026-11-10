"""What the tool gate decided, call by call, for the policy parity check (app/evals/decisions.py).

Nothing is recorded unless a recording is open: the hook in ``Runtime._run_tool`` and in the approval queue is one
read of a module list in production (not a ``ContextVar``: the test client runs the app in a thread of its own).
A record is the decision as the caller sees it: allowed, sent to a person, refused (with the words a customer or the
model reads) or answered with a question (``confirm_order`` / ``awaiting_review``).
Ids and clock times are replaced by placeholders, so two runs of the same code record the same lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_SINK: list[list[dict[str, Any]]] = []  # the open recordings, innermost last
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2}")
_IDS = re.compile(r"#\d+|(?<=审批单 #)\d+|V-\d+")


def stable(text: Any) -> str:
    return _IDS.sub("#<id>", _CLOCK.sub("<t>", str(text or "")))


def _amount(value: Any) -> str | None:
    try:
        return f"{float(value):.2f}" if value not in (None, "") else None
    except (TypeError, ValueError):
        return str(value)


@contextmanager
def recording() -> Iterator[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    _SINK.append(records)
    try:
        yield records
    finally:
        _SINK.remove(records)


def active() -> bool:
    return bool(_SINK)


def tool_call(tool: str, role: str, args: dict[str, Any], kind: str, output: dict[str, Any] | None, is_error: bool,
              reason: str = "", approver_role: str | None = None) -> None:
    if not _SINK:
        return
    sink = _SINK[-1]
    output = output or {}
    if kind == "approval":
        effect, detail = "review", reason
    elif output.get("confirm_order"):
        effect, detail = "ask", output.get("error", "")
    elif output.get("awaiting_review"):
        effect, detail = "waiting", output.get("error", "")
    elif is_error:
        effect, detail = "deny", output.get("error", "")
    else:
        effect, detail = "allow", ""
    sink.append({"tool": tool, "role": role, "order": str(args.get("order_id") or "") or None,
                 "amount": _amount(args.get("amount")), "effect": effect, "detail": stable(detail),
                 "approver": approver_role})


def approval_decision(role: str, own: bool, decision: str, required_role: str, status: int, detail: str = "") -> None:
    if not _SINK:
        return
    sink = _SINK[-1]
    sink.append({"tool": f"approval.{decision}", "role": role, "own": own, "required": required_role,
                 "effect": "allow" if status == 200 else f"deny:{status}", "detail": stable(detail)})
