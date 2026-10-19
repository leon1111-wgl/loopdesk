from __future__ import annotations

import hmac

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from ..channels.feishu import deduper, handle_message, parse_text_event
from ..db import new_session
from ..services import get_services

router = APIRouter(prefix="/api/channels", tags=["channels"])


def _first_delivery(event_id: str) -> bool:
    with new_session() as db:
        return deduper.first_time(db, "feishu", event_id)


@router.post("/feishu/{tenant_slug}/events")
async def feishu_events(tenant_slug: str, request: Request, background: BackgroundTasks) -> dict:
    body = await request.json()
    if "encrypt" in body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "暂不支持加密事件，请在飞书开放平台关闭 Encrypt Key")
    settings = get_services().settings
    expected = settings.feishu_verification_token
    if not expected and not settings.seed_demo:
        # Fail closed: without the token anyone who knows the URL could post fake customer
        # messages. The demo accepts unsigned events so the channel can be tried locally.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "飞书渠道未配置 verification token")
    token = body.get("token") or (body.get("header") or {}).get("token") or ""
    if expected and not hmac.compare_digest(str(token), expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification token 不匹配")
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}

    parsed = parse_text_event(body)
    if parsed is None:
        return {"code": 0, "msg": "ignored"}
    if not await run_in_threadpool(_first_delivery, parsed["event_id"]):
        return {"code": 0, "msg": "duplicate"}
    # Feishu expects a response within 3 seconds; the agent runs after we acknowledge.
    background.add_task(handle_message, get_services(), tenant_slug, parsed)
    return {"code": 0}
