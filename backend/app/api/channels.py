from __future__ import annotations

import hmac

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from ..channels.feishu import deduper, parse_text_event, process_event
from ..db import new_session
from ..services import get_services

router = APIRouter(prefix="/api/channels", tags=["channels"])


def _accept(event_id: str, payload: dict) -> bool:
    with new_session() as db:
        return deduper.first_time(db, "feishu", event_id, payload)


@router.post("/feishu/{tenant_slug}/events")
async def feishu_events(tenant_slug: str, request: Request, background: BackgroundTasks) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请求体必须是 JSON 对象")
    if "encrypt" in body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "暂不支持加密事件，请在飞书开放平台关闭 Encrypt Key")
    settings = get_services().settings
    app = settings.feishu_apps.get(tenant_slug)
    if app is None and not settings.seed_demo:
        # Fail closed: without this tenant's token anyone who knows the URL could post fake
        # customer messages. The demo accepts unsigned events so the channel can be tried locally.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "该租户的飞书渠道未配置")
    if app is not None:
        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        token = body.get("token") or header.get("token") or ""
        # The token proves the event came from Feishu; it is checked against this tenant's app
        # only, so an event signed for one tenant is refused at another tenant's URL.
        if not hmac.compare_digest(str(token), app.verification_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "verification token 不匹配")
        if header.get("app_id") not in (None, app.app_id):  # schema 2.0 names the sending app
            raise HTTPException(status.HTTP_403_FORBIDDEN, "事件不属于该租户的飞书应用")
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}

    parsed = parse_text_event(body)
    if parsed is None:
        return {"code": 0, "msg": "ignored"}
    # Stored before the acknowledgement, so a crash after it loses nothing (see retry_stuck).
    if not await run_in_threadpool(_accept, parsed["event_id"], {**parsed, "tenant": tenant_slug}):
        return {"code": 0, "msg": "duplicate"}
    # Feishu expects a response within 3 seconds; the agent runs after we acknowledge.
    background.add_task(process_event, get_services(), parsed["event_id"])
    return {"code": 0}
