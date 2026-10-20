"""End agent runs that a dead process left "running".

    python -m app.ops.stale_runs      # every minute, e.g. from cron or a Kubernetes CronJob

A run lives in one process (a streamed run in a daemon thread, see api/chat.py). When that
process is killed mid-run (kill -9, OOM, a recycled container, a deploy that does not wait),
no except or finally runs: the row stays "running" for good, counts in the metrics, its trace
never ends, and a customer who was just refunded never gets an answer. AgentRuntime ends its
runs on any error while the process lives; this sweep covers the process that is gone.

It goes by time, never by "everything running at startup": with several workers or instances,
the runs another live instance is executing are "running" too. Each run is ended with one
conditional UPDATE, so sweeps started from several places at once end it once. Every instance
also sweeps once when it starts (main.py), which only finds runs abandoned long enough ago.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import telemetry
from ..agent.runtime import REWRITE_BUDGET_SECONDS
from ..config import Settings
from ..db import new_session
from ..models import Approval, Conversation, Message, Run

INTERRUPTED_ERROR = "interrupted: the process ended before the run finished"
INTERRUPTED_TEXT = ("抱歉，系统处理您的请求时意外中断，这次没有完成回复。如果涉及退款等操作，结果以订单状态为准；"
                    "您可以重新发送，或转人工客服。")
CALL_ATTEMPTS = 3  # one model call: the first try and two retries (openai_compat, the Anthropic SDK)

logger = logging.getLogger(__name__)


def stale_after(settings: Settings) -> timedelta:
    """How long a live process can keep a run "running"; one running longer was abandoned.

    The run deadline bounds the agent loop: every model call in it is capped to the time left,
    and no step or retry starts after it. Outside the loop come the follow-up rewrite before it
    (capped at REWRITE_BUDGET_SECONDS) and, with LOOPDESK_VERIFIER=llm, the judge after it: a
    model call without a cap, up to 3 attempts of LOOPDESK_LLM_TIMEOUT. It is counted whatever
    the verifier setting, since the job that sweeps may not share the app's configuration. A
    minute more covers tools, commits and lock waits. Defaults: 45 s + 5 s + 3 × 60 s + 60 s =
    290 s. With the deadline disabled, the loop is bounded by its steps only (each one: both
    model tiers, 3 attempts each).

    Too short, and a run that is still going is failed: the customer is told it broke, then
    gets the answer, and the run's final write flips its status back. Too long only delays
    the cleanup."""
    call = CALL_ATTEMPTS * settings.llm_timeout
    deadline = settings.run_deadline_seconds
    loop = deadline if deadline > 0 else settings.max_agent_steps * 2 * call
    return timedelta(seconds=loop + REWRITE_BUDGET_SECONDS + call + 60)


def _stale(cutoff: datetime) -> tuple:
    """Still "running", and since before the cutoff. A run is running since it was created, or
    since the approval decision it was resumed with: resume() stamps decided_at and sets the
    run back to "running" in one commit, while the run itself may be days old."""
    resumed_since = select(Approval.id).where(Approval.run_id == Run.id, Approval.decided_at >= cutoff).exists()
    return Run.status == "running", Run.created_at < cutoff, ~resumed_since


def fail_stale_runs(settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """Mark abandoned runs failed, export their traces, and leave a notice for the customers
    still waiting on them. ``now`` (tests) only decides what counts as stale."""
    cutoff = (now or datetime.now(timezone.utc)) - stale_after(settings)
    with new_session() as db:
        run_ids = db.execute(select(Run.id).where(*_stale(cutoff))).scalars().all()
    counts = {"failed": 0, "notified": 0}
    for run_id in run_ids:
        with new_session() as db:
            if not _claim(db, run_id, cutoff):
                continue  # another sweep ended it first, or it was resumed meanwhile
            run = db.get(Run, run_id)
            notified = _notify(db, run)
            db.commit()  # the status and the notice together, or neither
            logger.warning("agent run %s was left running by a process that ended; marked failed", run_id)
            telemetry.export_run(db, run, settings)
        counts["failed"] += 1
        counts["notified"] += notified
    return counts


def _claim(db: Session, run_id: str, cutoff: datetime) -> bool:
    """One conditional UPDATE: of two sweeps that found the same run, exactly one ends it."""
    return db.execute(
        update(Run).where(Run.id == run_id, *_stale(cutoff))
        .values(status="failed", error=INTERRUPTED_ERROR, finished_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    ).rowcount == 1


def _notify(db: Session, run: Run) -> bool:
    """Tell the customer, if they are still waiting on this run: its question is the newest
    message in the conversation, or, for a run resumed after an approval, its own "your request
    is being reviewed" message is (the console keeps polling while that is the last word).

    Not once the conversation has moved on (an apology after a newer answer only confuses), not
    while a person has it, and not on Feishu, where the channel inbox answers the message again
    (app.ops.channel_retry). A system notice, not an answer: it stays out of the model's history
    and cannot be rated into the feedback flywheel."""
    if run.conversation_id is None or run.is_eval:
        return False
    conversation = db.get(Conversation, run.conversation_id)
    if conversation.channel not in ("web", "api") or conversation.status == "handoff":
        return False
    newest = db.execute(select(Message).where(Message.conversation_id == conversation.id)
                        .order_by(Message.id.desc()).limit(1)).scalar_one_or_none()
    asked_again = db.execute(select(Run.id).where(Run.conversation_id == conversation.id,
                                                  Run.created_at > run.created_at).limit(1)).first()
    if newest is None or not (newest.run_id == run.id or (newest.role == "user" and asked_again is None)):
        return False
    db.add(Message(tenant_id=run.tenant_id, conversation_id=conversation.id, role="system",
                   content=INTERRUPTED_TEXT, run_id=run.id))
    conversation.updated_at = datetime.now(timezone.utc)
    return True


def main() -> int:  # pragma: no cover - CLI wrapper
    from ..config import get_settings
    from ..db import init_engine

    settings = get_settings()
    init_engine(settings.database_url)
    telemetry.configure(settings)  # a swept run's trace is exported like any other ended run's
    print(json.dumps(fail_stale_runs(settings)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
