"""What a model error means, from the message the adapters write. The dashboard uses it to say why
runs fail (N-037); the pre-launch check uses it to say what to fix (N-032)."""

from __future__ import annotations

import re

_HTTP_STATUS = re.compile(r"\b(?:HTTP|Error code:) ?(\d{3})\b")

HINTS = {
    "rejected": "请求被拒绝：401/403 多半是密钥不对或没有权限，404 多半是模型名或地址不对，"
                "400/422 多半是参数不被支持（例如 stream_options，可设 LOOPDESK_LLM_STREAM_USAGE=0）。重试没有用",
    "unavailable": "服务不可用：5xx、429（限流或额度用完）或连不上地址，等它恢复，或检查网络和地址",
    "timeout": "超时：模型响应太慢或网络不通，检查 LLM_BASE_URL 和网络",
    "breaker": "熔断中：这个模型最近连续失败，暂时不调用",
    "disconnected": "顾客在运行结束前断开了连接",
    "interrupted": "进程在运行途中退出（被杀掉、OOM、发布时没等请求结束），收尾任务把运行记为失败",
    "other": "其他错误：看链路里的错误信息",
}


def error_kind(error: str | None) -> str:
    """A 4xx other than 408/409/429 is a request the service refused, usually our configuration or
    parameters: retrying does not help, so it is told apart from an unavailable service."""
    text = error or ""
    if text.startswith("circuit open"):
        return "breaker"
    if text.startswith("client disconnected"):
        return "disconnected"
    if text.startswith("embedding unavailable"):  # the search ran on BM25 only (N-078)
        return "embedding"
    if text.startswith("interrupted"):  # app.ops.stale_runs: the process running it is gone
        return "interrupted"
    status = _HTTP_STATUS.search(text)
    if status:
        code = int(status.group(1))
        return "unavailable" if code >= 500 or code in (408, 409, 429) else "rejected"
    lowered = text.lower()
    if "deadline" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if lowered.startswith(("network error", "stream interrupted", "connection error")):
        return "unavailable"
    return "other"
