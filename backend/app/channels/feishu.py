"""Feishu (飞书) bot channel.

Inbound: the event subscription posts to /api/channels/feishu/{tenant}/events.
- ``url_verification``: echo the challenge (after checking the verification token);
- ``im.message.receive_v1``: acknowledge within 3 seconds, process in the background,
  reply to the original message. Feishu retries unacknowledged events, so event IDs
  are de-duplicated.
Outbound: tenant_access_token (cached until expiry) + reply / send-to-chat APIs.

Without LOOPDESK_FEISHU_APP_ID / _APP_SECRET, replies are dry runs (returned, not sent).
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import OrderedDict

import httpx
from sqlalchemy import select

from ..agent.runtime import collect
from ..config import Settings
from ..db import new_session
from ..models import Conversation, Tenant, User
from ..security import hash_password

API = "https://open.feishu.cn/open-apis"
_MENTION = re.compile(r"@_user_\d+\s*")


class EventDeduper:
    def __init__(self, capacity: int = 2048):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def first_time(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen[event_id] = time.time()
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return True


deduper = EventDeduper()


class FeishuClient:
    _token: tuple[str, float] | None = None
    _lock = threading.Lock()

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.settings.feishu_app_id and self.settings.feishu_app_secret)

    def _tenant_token(self, client: httpx.Client) -> str:
        with FeishuClient._lock:
            if FeishuClient._token and FeishuClient._token[1] > time.time() + 60:
                return FeishuClient._token[0]
            response = client.post(f"{API}/auth/v3/tenant_access_token/internal", json={
                "app_id": self.settings.feishu_app_id, "app_secret": self.settings.feishu_app_secret,
            })
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"feishu token error: {data.get('msg')}")
            FeishuClient._token = (data["tenant_access_token"], time.time() + int(data.get("expire", 7200)))
            return FeishuClient._token[0]

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


def parse_text_event(body: dict) -> dict | None:
    """Extract what we need from an im.message.receive_v1 (schema 2.0) event."""
    if body.get("header", {}).get("event_type") != "im.message.receive_v1":
        return None
    event = body.get("event") or {}
    message = event.get("message") or {}
    if message.get("message_type") != "text":
        return None
    try:
        text = json.loads(message.get("content") or "{}").get("text", "")
    except json.JSONDecodeError:
        return None
    return {
        "event_id": body["header"].get("event_id", ""),
        "open_id": ((event.get("sender") or {}).get("sender_id") or {}).get("open_id", ""),
        "chat_id": message.get("chat_id", ""),
        "message_id": message.get("message_id", ""),
        "text": _MENTION.sub("", text).strip(),
    }


def handle_message(services, tenant_slug: str, parsed: dict) -> dict:
    """Run the agent for one Feishu message and reply. Executed as a background task."""
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == tenant_slug)).scalar_one_or_none()
        if tenant is None or not parsed["text"]:
            return {"skipped": True}
        email = f"feishu_{parsed['open_id']}@{tenant_slug}.channel".lower()
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(tenant_id=tenant.id, email=email, name=f"飞书用户{parsed['open_id'][-4:]}", role="customer",
                        password_hash=hash_password(secrets.token_urlsafe(24)))
            db.add(user)
            db.commit()
        conversation = db.execute(select(Conversation).where(
            Conversation.tenant_id == tenant.id, Conversation.channel == "feishu",
            Conversation.external_id == parsed["chat_id"], Conversation.user_id == user.id,
        )).scalar_one_or_none()
        if conversation is None:
            conversation = Conversation(tenant_id=tenant.id, user_id=user.id, channel="feishu",
                                        external_id=parsed["chat_id"], title="飞书会话")
            db.add(conversation)
            db.commit()
        summary = collect(services.runtime(db).start(tenant=tenant, user=user, text=parsed["text"],
                                                     conversation=conversation))
        answer = (summary["final"] or {}).get("text", "")
        return FeishuClient(services.settings).reply(parsed["message_id"], answer)
