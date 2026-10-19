"""Request IDs, structured access logs and the unhandled-error contract.

Every request gets an ID (the caller's ``X-Request-ID`` if it is a sane token, otherwise a
new one). It is returned in the response header, attached to every log line, stored in
the agent run's state, and included in 500 responses, so a user reporting "it broke" can
quote one ID that leads straight to the logs and the trace.

Logs are one JSON object per line (LOOPDESK_LOG_FORMAT=json, the default) so they can be
shipped to ELK / Loki / Cloud Logging without parsing rules.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
tenant_var: ContextVar[int | None] = ContextVar("tenant_id", default=None)
user_var: ContextVar[int | None] = ContextVar("user_id", default=None)

logger = logging.getLogger("loopdesk")
access_logger = logging.getLogger("loopdesk.access")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "tenant_id": tenant_var.get(),
            "user_id": user_var.get(),
        }
        for key in ("method", "path", "status", "latency_ms", "run_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(fmt: str = "json", level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("loopdesk")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        tenant_token, user_token = tenant_var.set(None), user_var.set(None)
        started = time.perf_counter()
        status = 500
        try:
            try:
                response = await call_next(request)
            except Exception:
                # Handled here, while the request id is still in context (an app-level
                # exception handler runs outside this middleware, after the reset).
                logger.exception("unhandled error on %s %s", request.method, request.url.path)
                response = _error_response(request_id)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            if request.url.path.startswith("/api"):
                access_logger.info(
                    "request", extra={"method": request.method, "path": request.url.path, "status": status,
                                      "latency_ms": int((time.perf_counter() - started) * 1000)},
                )
            request_id_var.reset(token)
            tenant_var.reset(tenant_token)
            user_var.reset(user_token)


def _error_response(request_id: str) -> JSONResponse:
    return JSONResponse(
        {"detail": "服务器内部错误，请稍后重试；如需排查，请提供 request_id", "request_id": request_id},
        status_code=500,
    )


def install(app: FastAPI) -> None:
    app.add_middleware(RequestContextMiddleware)
