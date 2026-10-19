"""WeCom (企业微信) group-robot notifications for pending approvals.

Uses the documented group-robot webhook:
POST https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=KEY
with a markdown message. Without LOOPDESK_WECOM_WEBHOOK_KEY the message is only
recorded in the trace (dry run), so the demo never calls out to the internet.
"""

from __future__ import annotations

import httpx

from ..config import Settings
from ..models import Approval, Tenant

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def approval_markdown(approval: Approval, tenant: Tenant) -> str:
    args = approval.arguments or {}
    return (
        f"### 【{tenant.name}】待审批：{approval.tool_name}\n"
        f"> 订单：<font color=\"info\">{args.get('order_id', '-')}</font>\n"
        f"> 金额：<font color=\"warning\">¥{float(args.get('amount') or 0):.2f}</font>\n"
        f"> 原因：{approval.reason}\n"
        f"请到 LoopDesk「审批队列」处理（审批单 #{approval.id}）。"
    )


def notify_approval(settings: Settings, approval: Approval, tenant: Tenant,
                    transport: httpx.BaseTransport | None = None) -> dict:
    content = approval_markdown(approval, tenant)
    if not settings.wecom_webhook_key:
        return {"sent": False, "mode": "dry_run", "content": content}
    try:
        with httpx.Client(timeout=5.0, transport=transport) as client:
            response = client.post(
                WEBHOOK_URL,
                params={"key": settings.wecom_webhook_key},
                json={"msgtype": "markdown", "markdown": {"content": content}},
            )
        body = response.json()
        return {"sent": body.get("errcode") == 0, "mode": "live", "errcode": body.get("errcode")}
    except (httpx.HTTPError, ValueError) as exc:
        return {"sent": False, "mode": "live", "error": str(exc)}
