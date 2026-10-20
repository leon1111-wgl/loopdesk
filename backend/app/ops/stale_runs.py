"""End agent runs that a dead process left "running".

    python -m app.ops.stale_runs      # every minute, e.g. from cron or a Kubernetes CronJob

A run lives in one process (a streamed run in a daemon thread, see api/chat.py). When that
process is killed mid-run (kill -9, OOM, a recycled container, a deploy that does not wait),
no except or finally runs: the row stays "running" for good, counts in the metrics, its trace
never ends, and a customer who was just refunded never gets an answer. AgentRuntime ends its
runs on any error while the process lives; this sweep covers the process that is gone.

A process can also die while it carries out a reviewer's decision (AgentRuntime.resume): the
approval is claimed by then, so nobody can decide it again. The sweep settles the decision with
the run (_settle): an approval whose tool had not run goes back to pending, one whose tool ran
stands and the customer gets the tool's result, a rejection stands and the customer gets it.

It goes by time, never by "everything running at startup": with several workers or instances,
the runs another live instance is executing are "running" too. Each run is ended with one
conditional UPDATE, and what ending it settles commits with it, so sweeps started from several
places at once end it, and reopen its approval, once. Every instance also sweeps once when it
starts (main.py), which only finds runs abandoned long enough ago.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import audit, telemetry
from ..agent.business_tools import build_registry
from ..agent.runtime import APPROVED_FALLBACK_TEXT, REWRITE_BUDGET_SECONDS, rejected_result
from ..agent.tools import ToolRegistry
from ..agent.tracing import Tracer
from ..channels.feishu import FeishuClient
from ..config import Settings
from ..db import new_session
from ..models import Approval, Conversation, Message, Run, Tenant, User
from ..security import Principal

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
    the cleanup.

    With a merchant's own order and payment systems (LOOPDESK_COMMERCE_APIS), a resumed approval
    reads the order and files the refund before its loop, outside the deadline: two of the slowest
    tenant's calls (CommerceApi.call_seconds) are added."""
    call = CALL_ATTEMPTS * settings.llm_timeout
    deadline = settings.run_deadline_seconds
    loop = deadline if deadline > 0 else settings.max_agent_steps * 2 * call
    commerce = 2 * max((api.call_seconds for api in settings.commerce_apis.values()), default=0.0)
    return timedelta(seconds=loop + REWRITE_BUDGET_SECONDS + call + 60 + commerce)


def _stale(cutoff: datetime) -> tuple:
    """Still "running", and since before the cutoff. A run is running since it was created, or
    since the approval decision it was resumed with: resume() stamps decided_at and sets the
    run back to "running" in one commit, while the run itself may be days old."""
    resumed_since = select(Approval.id).where(Approval.run_id == Run.id, Approval.decided_at >= cutoff).exists()
    return Run.status == "running", Run.created_at < cutoff, ~resumed_since


def fail_stale_runs(settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """Mark abandoned runs failed, export their traces, and settle what each leaves behind: a
    customer still waiting, or a reviewer's decision it was carrying out (_settle). ``now``
    (tests) only decides what counts as stale."""
    cutoff = (now or datetime.now(timezone.utc)) - stale_after(settings)
    with new_session() as db:
        run_ids = db.execute(select(Run.id).where(*_stale(cutoff))).scalars().all()
    registry = build_registry()
    counts = {"failed": 0, "notified": 0, "answered": 0, "reopened": 0}
    for run_id in run_ids:
        with new_session() as db:
            if not _claim(db, run_id, cutoff):
                continue  # another sweep ended it first, or it was resumed meanwhile
            run = db.get(Run, run_id)
            outcome = _settle(db, run, registry, settings)
            db.commit()  # the status and what it settles together (notice, answer, reopened approval), or none
            logger.warning("agent run %s was left running by a process that ended; marked failed", run_id)
            telemetry.export_run(db, run, settings)
            if outcome == "answered":
                _send_on_feishu(db, run, settings)
        counts["failed"] += 1
        if outcome is not None:
            counts[outcome] += 1
    return counts


def _claim(db: Session, run_id: str, cutoff: datetime) -> bool:
    """One conditional UPDATE: of two sweeps that found the same run, exactly one ends it."""
    return db.execute(
        update(Run).where(Run.id == run_id, *_stale(cutoff))
        .values(status="failed", error=INTERRUPTED_ERROR, finished_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    ).rowcount == 1


def _settle(db: Session, run: Run, registry: ToolRegistry, settings: Settings) -> str | None:
    """What ending the run takes besides its status: "notified", "answered", "reopened" or nothing.

    A run that was carrying out a reviewer's decision leaves it claimed (approved or rejected), so
    nobody will make it again. Settled as resume() does when it fails in a live process:
    - approved, and the tool has not run: back to pending, to be approved again. No notice: the
      customer's last word from us says the request is under review, which is true again, and
      the next decision answers them. The tool's idempotency key keeps that to one refund.
    - approved, and the tool ran (money moved): the decision stands, and the customer gets the
      tool's own result instead of the answer the model did not write.
    - rejected: the decision stands as well. Nothing was to run, so it took effect when it was
      recorded. Reopening it would let a crash undo a decision, and nobody would know to make it
      again: this is minutes later (a live resume answers the reviewer's request with a 503, at
      once). The customer gets the rejection and the reviewer's comment, written for them.
    A tool that cannot tell whether it ran keeps the decision and the notice: reopening could run
    it twice."""
    approval = _resumed_approval(db, run)
    if approval is None:
        return "notified" if _notify(db, run) else None
    if approval.status == "rejected":
        return _answer(db, run, rejected_result(approval.tool_name, approval.comment)["message"])
    tool = registry.get(approval.tool_name)
    if tool is None or tool.executed is None:
        logger.error("approval %s: cannot tell whether %s ran before its run was interrupted; the decision "
                     "stands, check it by hand", approval.id, approval.tool_name)
        return "notified" if _notify(db, run) else None
    result = tool.executed(db, approval.id)
    if result is None:
        _reopen(db, run, approval, settings)
        return "reopened"
    return _answer(db, run, str(result.get("message") or APPROVED_FALLBACK_TEXT), approval.tool_name)


def _resumed_approval(db: Session, run: Run) -> Approval | None:
    """The decision this run was carrying out, if any: resume() claims the approval and sets the
    run back to "running" in one commit, and the run's state keeps the pending call until the
    resumed run is saved."""
    approval_id = ((run.state or {}).get("pending") or {}).get("approval_id")
    approval = db.get(Approval, approval_id) if approval_id is not None else None
    if approval is None or approval.run_id != run.id or approval.status not in ("approved", "rejected"):
        return None
    return approval


def _reopen(db: Session, run: Run, approval: Approval, settings: Settings) -> None:
    """Back to pending, as AgentRuntime._reopen does after a failure in a live process: the run
    stays failed but unfinished, and the next resume() carries on from the pending call its state
    still holds. The decision's audit entry stays; this one, under the reviewer whose decision did
    not take effect, says so."""
    reviewer = Principal(db.get(User, approval.reviewer_id), db.get(Tenant, approval.tenant_id))
    approval.status, approval.reviewer_id, approval.comment, approval.decided_at = "pending", None, "", None
    run.finished_at = None
    audit.record(db, reviewer, "approval.reopen", f"approval:{approval.id}", decision="approve", error="interrupted")
    tracer = Tracer(db, run, settings, buffered=True)  # the span commits with the rest
    with tracer.span("approval", "reopened", {"approval_id": approval.id}) as span:
        span.status = "error"
        span.set(error=INTERRUPTED_ERROR, approval_status="pending")
    tracer.flush()
    logger.warning("approval %s is pending again: its run was interrupted before %s ran", approval.id,
                   approval.tool_name)


def _answer(db: Session, run: Run, text: str, tool: str | None = None) -> str | None:
    """The decision's result as the run's answer, where resume() puts its fallback reply: in the
    conversation whatever happened there since (it is no apology; the customer needs to know)."""
    run.output_text = text
    if run.conversation_id is None:
        return None
    tools = list((run.state or {}).get("tools", [])) + ([tool] if tool else [])
    db.add(Message(tenant_id=run.tenant_id, conversation_id=run.conversation_id, role="assistant", content=text,
                   run_id=run.id, meta={"citations": [], "verification": None, "tools": tools,
                                        "status": "completed", "fallback": True}))
    db.get(Conversation, run.conversation_id).updated_at = datetime.now(timezone.utc)
    return "answered"


def _send_on_feishu(db: Session, run: Run, settings: Settings) -> None:
    """A Feishu customer does not watch the console: send them the answer too, best effort, as the
    approvals API does after a resume. It is in the conversation either way."""
    conversation = db.get(Conversation, run.conversation_id)
    if conversation.channel != "feishu" or not conversation.external_id:
        return
    try:
        FeishuClient(settings, db.get(Tenant, run.tenant_id).slug).send_to_chat(conversation.external_id,
                                                                                run.output_text)
    except Exception:
        logger.exception("feishu delivery of the answer of run %s failed", run.id)


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
