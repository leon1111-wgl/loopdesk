"""Whole conversations graded by where they end (backend/evals/journeys.json).

The gate cases (runner.py) grade one turn: its tools and its words, in a dry run that writes nothing. A journey runs several
turns for real, a person's decision included, and is graded by the state it leaves: the order's status, the refund ledger,
the approval queue, the tickets. A wrong end state is a wrong refund, however good the text reads.

The same file runs against the browser engine of the static demo (frontend/src/demo/engine/journeys.test.ts), so the two are
held to the same outcomes. With a configured model it says what that model does; tests/test_journeys.py runs the offline one.

    python -m app.evals.journeys --report journeys.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals"
PASSWORD = "loopdesk-demo"


def load() -> list[dict[str, Any]]:
    return json.loads((EVAL_DIR / "journeys.json").read_text(encoding="utf-8"))


def _readable(text: str) -> str:
    return "".join(text.split())


def state(db, tenant_slug: str, conversation_ids: set[int]) -> dict[str, Any]:
    """Where a tenant's data stands: what the "end" of a journey is compared with."""
    from ..models import Approval, Conversation, Message, Order, Refund, Tenant, Ticket

    tenant = db.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one()
    db.expire_all()
    orders = {o.id: o.status for o in db.execute(select(Order).where(Order.tenant_id == tenant.id)).scalars()}
    # Only what this ledger filed: the seed's old refunds carry no idempotency key.
    refunds = {r.order_id: r.amount for r in db.execute(select(Refund).where(
        Refund.tenant_id == tenant.id, Refund.idempotency_key.is_not(None))).scalars()}
    approvals: dict[str, int] = {}
    for a in db.execute(select(Approval).where(Approval.tenant_id == tenant.id, Approval.conversation_id.in_(conversation_ids))).scalars():
        approvals[a.status] = approvals.get(a.status, 0) + 1
    tickets = db.execute(select(Ticket).where(Ticket.tenant_id == tenant.id, Ticket.conversation_id.in_(conversation_ids), Ticket.kind != "handoff")).scalars().all()
    handoff = any(c.status == "handoff" for c in db.execute(select(Conversation).where(Conversation.id.in_(conversation_ids))).scalars())
    last = db.execute(select(Message).where(Message.conversation_id.in_(conversation_ids)).order_by(Message.id.desc()).limit(1)).scalar_one_or_none()
    return {"orders": orders, "refunds": refunds, "approvals": approvals, "tickets": len(tickets), "handoff": handoff,
            "last_message": last.content if last else ""}


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Orders are checked one by one; refunds and approvals as a whole: nothing more may have happened than expected."""
    wrong: list[str] = []
    for order, status in (expected.get("orders") or {}).items():
        if actual["orders"].get(order) != status:
            wrong.append(f"order {order}: {actual['orders'].get(order)}, expected {status}")
    if "refunds" in expected and {k: round(v, 2) for k, v in actual["refunds"].items()} != expected["refunds"]:
        wrong.append(f"refunds: {actual['refunds']}, expected {expected['refunds']}")
    if "approvals" in expected and actual["approvals"] != expected["approvals"]:
        wrong.append(f"approvals: {actual['approvals']}, expected {expected['approvals']}")
    if "tickets" in expected and actual["tickets"] != expected["tickets"]:
        wrong.append(f"tickets: {actual['tickets']}, expected {expected['tickets']}")
    if "handoff" in expected and actual["handoff"] != expected["handoff"]:
        wrong.append(f"handoff: {actual['handoff']}, expected {expected['handoff']}")
    if "last_message" in expected and expected["last_message"] not in actual["last_message"]:
        wrong.append(f"last message: {actual['last_message'][:80]}")
    return wrong


def says(answer: str, expected: str) -> bool:
    return any(_readable(option) in _readable(answer) for option in expected.split("|"))


def run_journey(client, new_session, journey: dict[str, Any]) -> dict[str, Any]:
    """One journey through the real API. ``new_session`` opens a database session to read the end state."""
    headers: dict[str, dict[str, str]] = {}
    conversations: dict[str, int] = {}
    wrong: list[str] = []

    def auth(email: str) -> dict[str, str]:
        if email not in headers:
            token = client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).json()["token"]
            headers[email] = {"Authorization": f"Bearer {token}"}
        return headers[email]

    def check(expected: dict[str, Any], where: str) -> None:
        with new_session() as db:
            found = state(db, journey["tenant"], set(conversations.values()))
        wrong.extend(f"{where}: {line}" for line in compare(expected, found))

    for number, step in enumerate(journey["steps"], start=1):
        where = f"step {number}"
        if "check" in step:
            check(step["check"], where)
        elif "say" in step:
            email = step["as"]
            if email not in conversations:
                conversations[email] = client.post("/api/conversations", json={}, headers=auth(email)).json()["id"]
            response = client.post(f"/api/conversations/{conversations[email]}/stream", json={"content": step["say"]}, headers=auth(email))
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            answer = next((e["text"] for e in events if e["type"] == "final"), "")
            asked = any(e["type"] == "approval_required" for e in events)
            if "says" in step and not says(answer, step["says"]):
                wrong.append(f"{where}: said {answer[:120]}")
            if "approval" in step and asked != step["approval"]:
                wrong.append(f"{where}: approval {asked}, expected {step['approval']}")
        else:
            email = step["as"]
            pending = client.get("/api/approvals?status_filter=all", headers=auth(email)).json()
            mine = [a for a in pending if a["conversation_id"] in conversations.values()]
            if not mine:
                wrong.append(f"{where}: nothing to decide")
                continue
            response = client.post(f"/api/approvals/{mine[0]['id']}/decide",
                                   json={"decision": step["decide"], "comment": step.get("comment", "")}, headers=auth(email))
            expected = step.get("refused", 200)
            if response.status_code != expected:
                wrong.append(f"{where}: HTTP {response.status_code}, expected {expected}")
    check(journey["end"], "end")
    return {"key": journey["key"], "passed": not wrong, "wrong": wrong}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - tests/test_journeys.py runs run_journey
    from fastapi.testclient import TestClient

    from ..config import Settings
    from ..db import new_session
    from ..main import create_app

    parser = argparse.ArgumentParser(description="Whole conversations graded by their end state")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--model", choices=["mock", "configured"], default="mock")
    args = parser.parse_args(argv)
    results = []
    for journey in load():
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.from_env() if args.model == "configured" else Settings()
            settings.database_url = f"sqlite:///{tmp}/journey.db"
            settings.seed_demo, settings.seed_demo_traffic, settings.prewarm_index = True, False, False
            settings.auto_migrate, settings.external_migrations = True, False
            with TestClient(create_app(settings)) as client:
                results.append(run_journey(client, new_session, journey))
    passed = sum(r["passed"] for r in results)
    print(f"journeys: {passed}/{len(results)}")
    for r in results:
        if not r["passed"]:
            print(f"FAIL {r['key']}: {'; '.join(r['wrong'])}")
    if args.report:
        args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
