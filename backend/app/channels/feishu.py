"""Feishu (飞书) bot channel.

Inbound: the event subscription posts to /api/channels/feishu/{tenant}/events.
- ``url_verification``: echo the challenge (after checking the verification token);
- ``im.message.receive_v1``: store the event, acknowledge within 3 seconds, answer it in
  the background and reply to the original message. Feishu re-sends events it did not see
  acknowledged, so event IDs are de-duplicated in the database, where every worker process
  sees them. The stored event is also an inbox: a worker claims it before answering, and
  if that worker dies after the acknowledgement (Feishu will not resend), ``retry_stuck``
  (run every minute by ``python -m app.ops.channel_retry``) claims it again.
Outbound: tenant_access_token (cached until expiry) + reply / send-to-chat APIs.

Each tenant has its own app (LOOPDESK_FEISHU_APPS): the event's verification token and app ID
must be that tenant's. A tenant without an app gets dry-run replies (returned, not sent).
Failures inside the background task are logged and answered with a fallback reply,
because FastAPI's BackgroundTasks would otherwise swallow them silently.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..agent.runtime import MODEL_ERROR_TEXT, collect
from ..config import Settings
from ..db import new_session
from ..models import ChannelEvent, Conversation, Tenant, User
from ..security import hash_password

API = "https://open.feishu.cn/open-apis"
_MENTION = re.compile(r"@_user_\d+\s*")
logger = logging.getLogger(__name__)


class EventDeduper:
    """Accepted event IDs, kept in the channel_events table for a day. An in-memory set
    only knows the events its own process received: with several workers, or after a
    restart, a re-sent event was answered twice. One insert-if-absent decides, so of two
    deliveries arriving at the same time exactly one goes ahead."""

    retention = timedelta(days=1)

    def __init__(self) -> None:
        self._swept: datetime | None = None  # last time this process deleted expired IDs

    def first_time(self, db: Session, channel: str, event_id: str, payload: dict | None = None) -> bool:
        """Store the event (with what is needed to answer it) unless it is already stored."""
        now = datetime.now(timezone.utc)
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        first = db.execute(
            insert(ChannelEvent).values(channel=channel, event_id=event_id, received_at=now,
                                        status="received", payload=payload)
            .on_conflict_do_nothing(index_elements=[ChannelEvent.channel, ChannelEvent.event_id])
            .returning(ChannelEvent.event_id)
        ).first() is not None
        if self._swept is None or now - self._swept > timedelta(hours=1):
            # Answered or given-up events only: an unanswered one is still work to do.
            db.execute(delete(ChannelEvent).where(ChannelEvent.received_at < now - self.retention,
                                                  ChannelEvent.status.in_(("done", "failed"))))
            self._swept = now
        db.commit()
        return first


deduper = EventDeduper()


class FeishuClient:
    """Sends as the tenant's own Feishu app. Tokens are cached per app: one shared cache let
    a second tenant's replies go out with the first tenant's token."""

    _tokens: dict[str, tuple[str, float]] = {}
    _lock = threading.Lock()

    def __init__(self, settings: Settings, tenant_slug: str, transport: httpx.BaseTransport | None = None):
        self.app = settings.feishu_apps.get(tenant_slug)
        self.transport = transport

    @property
    def configured(self) -> bool:
        return self.app is not None

    def _tenant_token(self, client: httpx.Client) -> str:
        app = self.app
        with FeishuClient._lock:
            cached = FeishuClient._tokens.get(app.app_id)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
            response = client.post(f"{API}/auth/v3/tenant_access_token/internal", json={
                "app_id": app.app_id, "app_secret": app.app_secret,
            })
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"feishu token error: {data.get('msg')}")
            FeishuClient._tokens[app.app_id] = (data["tenant_access_token"], time.time() + int(data.get("expire", 7200)))
            return data["tenant_access_token"]

    def _post(self, url: str, body: dict, params: dict | None = None) -> dict:
        if not self.configured:
            return {"sent": False, "mode": "dry_run", "body": body}
        with httpx.Client(timeout=10.0, transport=self.transport) as client:
            token = self._tenant_token(client)
            response = client.post(url, params=params, json=body, headers={"Authorization": f"Bearer {token}"})
            data = response.json()
        return {"sent": data.get("code") == 0, "mode": "live", "code": data.get("code"), "msg": data.get("msg")}

    def reply(self, message_id: str, text: str) -> dict:
        return self._post(f"{API}/im/v1/messages/{message_id}/reply",
                          {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)})

    def send_to_chat(self, chat_id: str, text: str) -> dict:
        return self._post(f"{API}/im/v1/messages", {
            "receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False),
        }, params={"receive_id_type": "chat_id"})


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def parse_text_event(body: dict) -> dict | None:
    """Extract what we need from an im.message.receive_v1 (schema 2.0) event. Malformed parts
    make it an ignored event, not a 500 (which Feishu would keep re-sending)."""
    header = _obj(body.get("header"))
    if header.get("event_type") != "im.message.receive_v1":
        return None
    event = _obj(body.get("event"))
    message = _obj(event.get("message"))
    if message.get("message_type") != "text":
        return None
    try:
        text = _obj(json.loads(message.get("content") or "{}")).get("text", "")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(text, str):
        return None
    event_id = header.get("event_id")
    if not isinstance(event_id, str) or not 0 < len(event_id) <= 128:
        # Without an ID a re-sent copy cannot be recognised; schema 2.0 events always carry one.
        logger.warning("feishu event without a usable event_id ignored")
        return None
    return {
        "event_id": event_id,
        "open_id": str(_obj(_obj(event.get("sender")).get("sender_id")).get("open_id") or ""),
        "chat_id": message.get("chat_id", ""),
        "message_id": message.get("message_id", ""),
        "text": _MENTION.sub("", text).strip(),
    }


MAX_ATTEMPTS = 3
QUEUED_GRACE = timedelta(seconds=30)  # an accepted event's own background task normally starts sooner


def stale_after(settings: Settings) -> timedelta:
    """How long a claim may last before its worker counts as dead: twice the run deadline,
    at least five minutes, so a slow run is never answered twice."""
    return timedelta(seconds=max(300.0, 2 * settings.run_deadline_seconds))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _claim(db: Session, event_id: str, now: datetime, stale_before: datetime):
    """One conditional UPDATE: an unclaimed event, or one whose worker died. Of two workers
    (the event's own task and the sweeper, or two sweepers) exactly one gets it."""
    row = db.execute(
        update(ChannelEvent).where(
            ChannelEvent.channel == "feishu", ChannelEvent.event_id == event_id,
            ChannelEvent.attempts < MAX_ATTEMPTS,
            or_(ChannelEvent.status == "received",
                and_(ChannelEvent.status == "processing", ChannelEvent.claimed_at < stale_before)),
        ).values(status="processing", attempts=ChannelEvent.attempts + 1, claimed_at=now)
        .returning(ChannelEvent.payload).execution_options(synchronize_session=False)
    ).first()
    db.commit()
    return row


def _without_text(payload: dict) -> dict:
    """What a settled event keeps: enough to recognise a redelivery, not the customer's words. The
    text is unmasked (the runtime masks it, and needs to see what it masked); the conversation
    keeps the masked copy, and the event row outlives the run by a day (N-083)."""
    return {k: v for k, v in payload.items() if k != "text"}


def _settle(event_id: str, status: str, payload: dict) -> None:
    with new_session() as db:
        db.execute(update(ChannelEvent).where(ChannelEvent.channel == "feishu", ChannelEvent.event_id == event_id)
                   .values(status=status, payload=_without_text(payload)).execution_options(synchronize_session=False))
        db.commit()


def process_event(services, event_id: str, now: datetime | None = None) -> dict | None:
    """Claim an accepted event and answer it; None when another worker has it. An event whose
    answer failed got the fallback reply (handle_message) and is settled as failed, not retried.
    ``now`` (a sweep's clock) only decides what counts as stale. The claim is stamped with the
    moment it is made: a sweep answering a backlog for minutes would otherwise back-date its
    later claims, and the next sweep would take them over while they are being answered."""
    now = now or _utcnow()
    with new_session() as db:
        claimed = _claim(db, event_id, _utcnow(), now - stale_after(services.settings))
    if claimed is None or not claimed.payload:
        return None
    payload = claimed.payload
    result = handle_message(services, payload["tenant"], payload)
    _settle(event_id, "failed" if result.get("fallback") else "done", payload)
    return result


def retry_stuck(services, now: datetime | None = None) -> dict[str, int]:
    """Answer accepted events that no worker finished: never started (the process died right
    after the acknowledgement) or claimed by a worker that died. After MAX_ATTEMPTS the
    customer gets the fallback reply instead of silence. Business effects of a repeated run
    are idempotent (refund claim, handoff ticket, pending-approval dedup); the reply itself
    can repeat only if a worker dies between replying and settling."""
    now = now or datetime.now(timezone.utc)
    stale_before = now - stale_after(services.settings)
    stuck = or_(and_(ChannelEvent.status == "received", ChannelEvent.received_at < now - QUEUED_GRACE),
                and_(ChannelEvent.status == "processing", ChannelEvent.claimed_at < stale_before))
    with new_session() as db:
        rows = db.execute(select(ChannelEvent.event_id, ChannelEvent.attempts)
                          .where(ChannelEvent.channel == "feishu", stuck)).all()
    counts = {"answered": 0, "gave_up": 0}
    for event_id, attempts in rows:
        if attempts < MAX_ATTEMPTS:
            counts["answered"] += process_event(services, event_id, now) is not None
            continue
        with new_session() as db:  # give up once, even with several sweepers
            payload = db.execute(
                update(ChannelEvent).where(ChannelEvent.channel == "feishu", ChannelEvent.event_id == event_id, stuck)
                .values(status="failed").returning(ChannelEvent.payload).execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            if payload:
                db.execute(update(ChannelEvent).where(ChannelEvent.channel == "feishu", ChannelEvent.event_id == event_id)
                           .values(payload=_without_text(payload)).execution_options(synchronize_session=False))
            db.commit()
        if payload:
            logger.error("feishu event %s gave up after %d attempts", event_id, attempts)
            FeishuClient(services.settings, payload["tenant"]).reply(payload["message_id"], MODEL_ERROR_TEXT)
            counts["gave_up"] += 1
    return counts


def handle_message(services, tenant_slug: str, parsed: dict) -> dict:
    """Run the agent for one Feishu message and reply. Executed as a background task:
    the HTTP response has already been sent, so an exception here would vanish without a
    trace and the customer would wait forever. Log it and send a fallback reply instead."""
    try:
        return _answer(services, tenant_slug, parsed)
    except Exception:
        logger.exception("feishu message %s for tenant %s failed", parsed.get("message_id"), tenant_slug)
        try:
            return {**FeishuClient(services.settings, tenant_slug).reply(parsed["message_id"], MODEL_ERROR_TEXT), "fallback": True}
        except Exception:
            logger.exception("feishu fallback reply to %s failed", parsed.get("message_id"))
            return {"sent": False, "error": "fallback reply failed", "fallback": True}


def _get_or_create(db: Session, query, make):
    found = db.execute(query).scalar_one_or_none()
    if found is not None:
        return found
    db.add(make())
    try:
        db.commit()
    except IntegrityError:  # created concurrently by another worker: use theirs
        db.rollback()
    return db.execute(query).scalar_one()


def _answer(services, tenant_slug: str, parsed: dict) -> dict:
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one_or_none()
        if tenant is None or not parsed["text"]:
            return {"skipped": True}
        email = f"feishu_{parsed['open_id']}@{tenant_slug}.channel".lower()
        # Get or create, both guarded by unique indexes: a customer's first two messages can be
        # answered at the same time (by two workers, or a worker and the retry sweeper).
        user = _get_or_create(db, select(User).where(User.email == email), lambda: User(
            tenant_id=tenant.id, email=email, name=f"飞书用户{parsed['open_id'][-4:]}", role="customer",
            password_hash=hash_password(secrets.token_urlsafe(24))))
        conversation = _get_or_create(db, select(Conversation).where(
            Conversation.tenant_id == tenant.id, Conversation.channel == "feishu",
            Conversation.external_id == parsed["chat_id"], Conversation.user_id == user.id,
        ), lambda: Conversation(tenant_id=tenant.id, user_id=user.id, channel="feishu",
                                external_id=parsed["chat_id"], title="飞书会话"))
        summary = collect(services.runtime(db).start(tenant=tenant, user=user, text=parsed["text"],
                                                     conversation=conversation))
        answer = (summary["final"] or {}).get("text") or MODEL_ERROR_TEXT
        return FeishuClient(services.settings, tenant_slug).reply(parsed["message_id"], answer)
