from __future__ import annotations

import contextvars
import json
import queue
import threading
from collections.abc import Callable, Iterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import handoff
from ..agent.runtime import RunOptions, collect
from ..config import get_settings
from ..db import get_db, new_session
from ..flywheel.service import FlywheelError, submit_feedback
from ..models import Conversation, Feedback, Message, Tenant, User
from ..security import Principal, get_principal, rate_limiter
from ..services import get_services
from .common import get_owned, iso

router = APIRouter(prefix="/api", tags=["chat"])


class ConversationIn(BaseModel):
    title: str | None = Field(default=None, max_length=100)


class ChatOptions(BaseModel):
    inject_hallucination: bool = False


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    options: ChatOptions = ChatOptions()


class QuickChatIn(ChatIn):
    conversation_id: int | None = None


class FeedbackIn(BaseModel):
    rating: str = Field(pattern="^(up|down|correction)$")
    correction: str = Field(default="", max_length=2000)
    reason_category: str = Field(default="", max_length=32)
    comment: str = Field(default="", max_length=500)


def conversation_out(c: Conversation) -> dict:
    return {"id": c.id, "title": c.title, "channel": c.channel, "status": c.status,
            "user_id": c.user_id, "created_at": iso(c.created_at), "updated_at": iso(c.updated_at)}


def message_out(m: Message, feedback: Feedback | None = None) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "run_id": m.run_id, "meta": m.meta or {},
            "created_at": iso(m.created_at),
            "feedback": {"rating": feedback.rating, "correction": feedback.correction} if feedback else None}


def _load_conversation(db: Session, principal: Principal, conversation_id: int) -> Conversation:
    conversation = get_owned(db, Conversation, conversation_id, principal.tenant.id)
    if conversation.user_id != principal.user.id and not principal.is_staff:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
    return conversation


def _options(principal: Principal, body: ChatIn) -> RunOptions:
    # Hallucination injection is a demo switch for the evidence verifier: admins only.
    inject = body.options.inject_hallucination and principal.user.role == "admin"
    return RunOptions(inject_hallucination=inject)


@router.get("/conversations")
def list_conversations(scope: str = "mine", principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)) -> list[dict]:
    query = select(Conversation).where(Conversation.tenant_id == principal.tenant.id, Conversation.channel != "eval")
    if scope != "all" or not principal.is_staff:
        query = query.where(Conversation.user_id == principal.user.id)
    rows = db.execute(query.order_by(Conversation.updated_at.desc()).limit(100)).scalars()
    return [conversation_out(c) for c in rows]


@router.post("/conversations")
def create_conversation(body: ConversationIn, principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)) -> dict:
    conversation = Conversation(tenant_id=principal.tenant.id, user_id=principal.user.id, title=body.title or "新对话")
    db.add(conversation)
    db.commit()
    return conversation_out(conversation)


@router.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)) -> dict:
    conversation = _load_conversation(db, principal, conversation_id)
    messages = db.execute(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
    ).scalars().all()
    # The reader's own feedback only: a support agent's correction of an answer (internal
    # flywheel data, possibly internal knowledge) is not shown to the customer who asked.
    feedback = {
        f.message_id: f for f in db.execute(
            select(Feedback).where(Feedback.message_id.in_([m.id for m in messages]),
                                   Feedback.user_id == principal.user.id).order_by(Feedback.id)
        ).scalars()
    }
    return {"conversation": conversation_out(conversation), "handoff": handoff.state(db, conversation),
            "messages": [message_out(m, feedback.get(m.id)) for m in messages]}


@router.post("/conversations/{conversation_id}/stream")
def stream_message(conversation_id: int, body: ChatIn, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)) -> StreamingResponse:
    """Server-Sent Events. Each event is ``data: {json}`` with a ``type`` field:
    run_started, guardrail, token, step_done, tool_call, tool_result, approval_required,
    verification, answer_replaced, final, done, error."""
    rate_limiter.check(f"chat:{principal.user.id}", get_settings().rate_limit_per_minute)
    _load_conversation(db, principal, conversation_id)
    options = _options(principal, body)
    user_id, tenant_id = principal.user.id, principal.tenant.id

    def run() -> Iterator[dict]:
        session = new_session()  # the request-scoped session is closed once streaming starts
        try:
            runtime = get_services().runtime(session)
            yield from runtime.start(
                tenant=session.get(Tenant, tenant_id), user=session.get(User, user_id),
                text=body.content, conversation=session.get(Conversation, conversation_id), options=options,
            )
        except Exception as exc:  # report instead of silently cutting the stream
            session.rollback()
            yield {"type": "error", "message": str(exc)[:300]}
        finally:
            session.close()

    def events() -> Iterator[str]:
        for event in detached(run):
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def detached(produce: Callable[[], Iterator[dict]]) -> Iterator[dict]:
    """Run a generator to completion in its own thread and relay its items.

    A customer who closes the tab mid-answer stops consuming the stream. Without this,
    the run would stop at its current step (the runtime records it as failed), with tool
    side effects (a refund) but no answer saved. Here the run always finishes: the answer is
    in the conversation when the customer comes back, and the trace is complete."""
    items: queue.Queue = queue.Queue()
    done = object()

    def worker() -> None:
        try:
            for item in produce():
                items.put(item)
        finally:
            items.put(done)

    # A new thread starts with an empty context: carry the request id (and tenant / user)
    # over, or the run's logs and trace lose their link to the request.
    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(worker,), name="loopdesk-run", daemon=True).start()
    while (item := items.get()) is not done:
        yield item


@router.post("/chat")
def chat(body: QuickChatIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    """Non-streaming variant for API integrations and scripts."""
    rate_limiter.check(f"chat:{principal.user.id}", get_settings().rate_limit_per_minute)
    if body.conversation_id:
        conversation = _load_conversation(db, principal, body.conversation_id)
    else:
        conversation = Conversation(tenant_id=principal.tenant.id, user_id=principal.user.id, channel="api")
        db.add(conversation)
        db.commit()
    summary = collect(get_services().runtime(db).start(
        tenant=principal.tenant, user=principal.user, text=body.content,
        conversation=conversation, options=_options(principal, body),
    ))
    final = summary["final"] or {}
    return {
        "conversation_id": conversation.id,
        "run_id": final.get("run_id"),
        "message_id": final.get("message_id"),
        "answer": final.get("text"),
        "status": final.get("status"),
        "tools": summary["tool_calls"],
        "approval": summary["approval"],
        "verification": summary["verification"],
        "citations": (final.get("meta") or {}).get("citations", []),
        "usage": summary["done"],
    }


@router.post("/messages/{message_id}/feedback")
def feedback(message_id: int, body: FeedbackIn, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)) -> dict:
    message = get_owned(db, Message, message_id, principal.tenant.id)
    conversation = db.get(Conversation, message.conversation_id)
    if conversation.user_id != principal.user.id and not principal.is_staff:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资源不存在")
    services = get_services()
    try:
        return submit_feedback(
            db, user=principal.user, message=message, rating=body.rating, correction=body.correction,
            reason_category=body.reason_category, comment=body.comment,
            provider=services.provider, model=services.settings.llm_model_strong,
        )
    except FlywheelError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
