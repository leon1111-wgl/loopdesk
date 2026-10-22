"""Tenant-tool writes a process left half done: hand them to a person.

A write or money call is recorded (pending, with its idempotency key) before it is sent. If the
process dies between sending and saving the answer, the row stays pending and nobody knows whether
the merchant's system did it. Every few minutes this marks such rows "unknown" and opens a ticket,
as a live call does when it gets no usable answer (tooling/http_tool.py). It never re-sends: unlike
the refund contract, a tenant's endpoint is not promised to honour the key forever.

    python -m app.ops.tool_reconcile          # cron: */5 * * * *
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..db import new_session
from ..models import Approval, ToolExecution
from ..tooling.http_tool import check_ticket

STALE_AFTER = timedelta(minutes=10)  # longer than any call can take (timeout × attempts ≤ 20 s)
logger = logging.getLogger(__name__)


def reconcile_tool_calls(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    done = 0
    with new_session() as db:
        rows = db.execute(select(ToolExecution).where(
            ToolExecution.status == "pending", ToolExecution.created_at < now - STALE_AFTER)
            .order_by(ToolExecution.id).limit(200)).scalars().all()
        for row in rows:
            approval = db.get(Approval, row.approval_id) if row.approval_id else None
            customer = row.customer_id or (approval.requested_by if approval else None)
            if customer is None:
                logger.error("tool call %s has no customer to file a ticket for", row.id)
                continue
            row.status, row.error, row.updated_at = "unknown", "进程在发送和保存结果之间中断", now
            check_ticket(db, row, row.tool_name, row.error, customer_id=customer)
            done += 1
        db.commit()
    return done


def main() -> None:  # pragma: no cover - CLI
    from ..config import get_settings
    from ..db import init_engine

    init_engine(get_settings().database_url)
    print(f"tool calls handed to a person: {reconcile_tool_calls()}")


if __name__ == "__main__":  # pragma: no cover
    main()
