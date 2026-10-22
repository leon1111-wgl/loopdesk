"""Calling a tenant's own endpoint for a tool it declared.

    "binding": {
      "method": "POST",
      "url": "https://api.harbourline.example/v1/accounts/{account_id}/fee-refunds",
      "auth": {"secret": "HARBOURLINE_API", "scheme": "bearer"},     # or {"scheme": "header", "header": "X-Api-Key"}
      "signing_secret": "HARBOURLINE_SIGNING",                       # optional HMAC-SHA256, as for commerce APIs
      "timeout": 5, "max_attempts": 3,
      "idempotency": "header",                                       # the endpoint honours Idempotency-Key
      "result": {"fields": ["status", "refund_id", "amount", "currency"],
                 "message": "已退还年费 {currency} {amount:.2f}，退款单号 {refund_id}。"},
      "errors": {"account_restricted": {"text": "该账户因安全原因暂时受限", "restriction": true}}
    }

What this side guarantees, on top of app/tooling/egress.py:
- the model's arguments are checked against the tool's schema first; path placeholders are filled
  with each value encoded as one path segment, the rest go in the query (GET) or a JSON body;
- every request carries the tenant, the request ID, and who is asking: the signed-in customer's
  identity (X-LoopDesk-Customer) from the session, never from the model;
- reads are retried after a timeout, a dropped connection or 408/425/429/5xx; a write or money call
  only when the endpoint honours Idempotency-Key, under the same key every time. The key is stored in
  the tool-call ledger (models.ToolExecution) before the first attempt, so a retried approval, a second
  click or a crash between sending and saving sends the same key again;
- a write whose outcome is unknown (no answer, an unknown 409/422) is never reported as done or as
  refused: the customer is told it is being checked and a person gets a ticket;
- a refusal comes back with the tenant's own wording for its code; a code marked as a restriction
  (an account frozen for fraud risk) also carries the appeal path (反电信网络诈骗法 §32(3): tell the
  customer the reason and how to appeal);
- replies are capped (256 KB), must be JSON objects, and only the declared fields reach the model;
- in a dry run (evals, staff tests) a write never reaches a real system: only the synthetic merchants
  of the demo tenants get it, marked X-LoopDesk-Dry-Run, and they change nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select

from ..agent.tools import ToolContext, ToolError, ToolUnavailable
from ..integrations.base import customer_ref, deadline, record
from ..models import Ticket, ToolExecution
from ..observability import request_id_var
from . import secrets
from .egress import EgressError, EgressPolicy
from .schema import SchemaError, validate_arguments

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_REPLY_BYTES = 256 * 1024
BACKOFF_SECONDS = 0.2
_sleep = time.sleep  # tests replace it
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_TEMPLATE_FIELD = re.compile(r"\{([a-z][a-z0-9_]*)(?::(\.\d+f|,\.\d+f|d))?\}")

logger = logging.getLogger(__name__)


class ToolRefusal(ToolError):
    """The endpoint answered and refused. ``output`` is what the model reads (the tenant's wording,
    and for a restriction the reason and the appeal path)."""

    def __init__(self, message: str, output: dict[str, Any]):
        super().__init__(message)
        self.output = output


class BindingError(ValueError):
    pass


class NotSent(ToolUnavailable):
    """Refused before anything left: the egress policy, a missing secret. Nothing can have been done."""


def check_binding(binding: dict[str, Any], parameters: dict[str, Any], hosts: list[str] | tuple[str, ...]) -> dict:
    """The binding as stored, or BindingError: the method, a URL the egress policy accepts, placeholders
    that are declared parameters, a secret reference (never a value), sane timeouts."""
    binding = dict(binding or {})
    method = str(binding.get("method", "GET")).upper()
    if method not in METHODS:
        raise BindingError(f"method 只能是 {' / '.join(METHODS)}")
    binding["method"] = method
    url = str(binding.get("url") or "")
    try:
        EgressPolicy(hosts).check_template(url)
    except EgressError as exc:
        raise BindingError(f"url 不被允许：{exc}") from None
    props = (parameters or {}).get("properties") or {}
    unknown = [p for p in _PLACEHOLDER.findall(url) if p not in props]
    if unknown:
        raise BindingError(f"url 里的占位符没有对应参数：{', '.join(unknown)}")
    optional = [p for p in _PLACEHOLDER.findall(url) if p not in ((parameters or {}).get("required") or [])]
    if optional:  # an absent value would leave "//" in the path
        raise BindingError(f"url 里的占位符必须是必填参数：{', '.join(optional)}")
    if "?" in url:
        raise BindingError("url 不能自带查询串；参数会按方法放进查询串或请求体")
    for key in ("auth", "signing_secret"):
        ref = (binding.get(key) or {}).get("secret") if key == "auth" else binding.get(key)
        if ref:
            try:
                secrets.check_name(ref)
            except secrets.SecretError as exc:
                raise BindingError(str(exc)) from None
    auth = binding.get("auth") or {}
    if auth and auth.get("scheme", "bearer") not in ("bearer", "header"):
        raise BindingError("auth.scheme 只能是 bearer 或 header")
    if auth.get("scheme") == "header" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,40}", str(auth.get("header") or "")):
        raise BindingError("auth.header 必须是合法的请求头名")
    timeout, attempts = float(binding.get("timeout", 5)), int(binding.get("max_attempts", 3))
    if not 0 < timeout <= 15 or not 1 <= attempts <= 4 or timeout * attempts > 20:
        raise BindingError("timeout 在 0–15 秒，max_attempts 在 1–4，两者乘积不超过 20 秒")
    binding["timeout"], binding["max_attempts"] = timeout, attempts
    if binding.setdefault("idempotency", "header") not in ("header", "none"):
        raise BindingError("idempotency 只能是 header（对方支持 Idempotency-Key）或 none")
    result = binding.get("result") or {}
    fields = result.get("fields")
    if fields is not None and (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)):
        raise BindingError("result.fields 必须是字段名列表")
    if "message" in result and not isinstance(result["message"], str):
        raise BindingError("result.message 必须是文字模板")
    errors = binding.get("errors") or {}
    if not isinstance(errors, dict) or not all(isinstance(v, dict) and isinstance(v.get("text"), str)
                                                for v in errors.values()):
        raise BindingError("errors 必须是 {错误码: {\"text\": 给顾客的说法}}")
    return binding


def render(template: str, data: dict[str, Any]) -> str | None:
    """"{currency} {amount:.2f}" filled from the reply's fields; None if a field is missing (the model
    then words it from the fields). No attribute or index access, unlike str.format."""
    missing = False

    def field(match: re.Match[str]) -> str:
        nonlocal missing
        value = data.get(match.group(1))
        spec = match.group(2)
        if value is None or isinstance(value, (dict, list)):
            missing = True
            return ""
        if spec and isinstance(value, (int, float)) and not isinstance(value, bool):
            return format(value, spec)
        return str(value)

    try:
        text = _TEMPLATE_FIELD.sub(field, template)
    except (ValueError, TypeError):  # "{amount:d}" over 30.0: the model words it from the fields instead
        return None
    return None if missing else text


def check_path_values(binding: dict[str, Any], args: dict[str, Any]) -> None:
    """A path segment cannot be empty or a dot segment: httpx resolves "/accounts/../fee-refunds" to
    "/fee-refunds", which would send the tenant's token to an endpoint the tool does not name."""
    for name in _PLACEHOLDER.findall(str(binding.get("url") or "")):
        if str(args.get(name, "")).strip() in ("", ".", ".."):
            raise ToolError(f"参数 {name} 的取值不能为空，也不能是 . 或 ..")


class HttpToolCaller:
    """Sends one tool call. ``row`` is the TenantTool; ``route`` gives the transport for a host
    (the synthetic merchants, a test's MockTransport) or None for the network, pinned by egress."""

    def __init__(self, ctx: ToolContext, row: Any, label: str):
        self.ctx, self.row, self.label = ctx, row, label
        self.binding = row.binding or {}
        self.commerce = ctx.commerce
        self.writes = row.risk in ("write", "money")

    # ------------------------------------------------------------ entry
    def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            args = validate_arguments(self.row.parameters, args)
        except SchemaError as exc:
            raise ToolError(str(exc)) from None
        check_path_values(self.binding, args)
        host = self._host()
        self._secrets()
        synthetic = self.commerce is not None and self.commerce.synthetic_host(self.ctx.tenant, host)
        if self.ctx.dry_run and self.writes and not synthetic:
            # A real endpoint may not honour a dry-run marker: it never gets a dry-run write.
            return {"status": "dry_run", "message": f"（演练）{self.label}已通过校验，未实际提交。"}
        if self.ctx.sandboxed and not synthetic:
            raise NotSent(f"演练环境中没有 {host} 的模拟系统")
        if not self.writes or self.ctx.dry_run:
            reply = self._send(args, host, key=None)
            return self._read(reply)
        return self._write(args, host)

    # ------------------------------------------------------------ writes and the ledger
    def _ledger_row(self) -> ToolExecution | None:
        if self.ctx.approval_id is None:
            return None
        return self.ctx.db.execute(select(ToolExecution).where(
            ToolExecution.approval_id == self.ctx.approval_id, ToolExecution.tool_name == self.row.name,
        )).scalars().first()

    def _write(self, args: dict[str, Any], host: str) -> dict[str, Any]:
        db = self.ctx.db
        row = self._ledger_row()
        if row is not None and row.status == "succeeded":
            return dict(row.result)
        if row is not None and row.status == "failed":
            raise ToolRefusal(row.error or f"{self.label}未能办理", dict(row.result) or {"error": row.error})
        if row is not None and row.status == "not_sent":  # refused before sending: try again under the key
            row.status = "pending"
        if row is None:
            row = ToolExecution(
                tenant_id=self.ctx.tenant.id, tool_id=self.row.id, tool_name=self.row.name,
                tool_version=self.row.version, run_id=self.ctx.run_id, approval_id=self.ctx.approval_id,
                conversation_id=self.ctx.conversation_id,
                customer_id=self.ctx.user.id if not self.ctx.is_staff else None,
                idempotency_key=f"ld-tool-{uuid.uuid4().hex}", arguments=args, status="pending",
            )
            db.add(row)
        db.commit()  # the key is durable before anything is sent
        try:
            reply = self._send(args, host, key=row.idempotency_key)
        except NotSent as exc:
            if row.attempts == 0:  # never sent under this key: nothing was done, and it may be sent later
                row.status, row.error = "not_sent", str(exc)[:255]
                db.commit()
            raise
        except (ToolUnavailable, _Unknown) as exc:
            # No usable answer: it may have been done. Keep the key, tell the customer the truth.
            return self._unknown(row, str(exc))
        except Exception as exc:  # anything else once a request may have left: never "try again"
            db.rollback()
            logger.exception("tool %s: sending failed unexpectedly", self.row.name)
            return self._unknown(db.merge(row), f"发送时出错：{type(exc).__name__}")
        row.attempts = (row.attempts or 0) + int(reply.call.get("attempts") or 1)
        row.http_status = reply.status
        try:
            output = self._read(reply)
        except ToolRefusal as refusal:
            row.status, row.error, row.result = "failed", str(refusal)[:255], refusal.output
            row.updated_at = datetime.now(timezone.utc)
            db.commit()
            raise
        except _Unknown as exc:
            return self._unknown(row, str(exc))
        except Exception as exc:  # the endpoint answered; reading the answer failed: it may well be done
            db.rollback()
            logger.exception("tool %s: reading the reply failed", self.row.name)
            return self._unknown(db.merge(row), f"处理回复时出错：{type(exc).__name__}")
        row.status, row.result, row.updated_at = "succeeded", output, datetime.now(timezone.utc)
        db.commit()
        return output

    def _unknown(self, row: ToolExecution, why: str) -> dict[str, Any]:
        db = self.ctx.db
        row.status, row.error, row.updated_at = "unknown", why[:255], datetime.now(timezone.utc)
        check_ticket(db, row, self.label, why, customer_id=self.ctx.user.id)
        db.commit()
        raise ToolError(unknown_message(self.label, row.ticket_id))

    # ------------------------------------------------------------ the request
    def _host(self) -> str:
        try:
            return EgressPolicy(self.ctx.tenant.tool_hosts or ()).check_template(self.binding.get("url", ""))
        except EgressError as exc:
            raise NotSent(f"{self.label}暂时不可用（出站策略：{exc}）") from None

    def _headers(self, key: str | None) -> dict[str, str]:
        ctx = self.ctx
        headers = {"Accept": "application/json", "User-Agent": "LoopDesk/1.0", "X-LoopDesk-Tenant": ctx.tenant.slug,
                   "X-LoopDesk-Tool": self.row.name}
        if ctx.is_staff:
            headers["X-LoopDesk-Staff"] = customer_ref(ctx.user)
            headers["X-LoopDesk-Role"] = ctx.user.role
        else:
            headers["X-LoopDesk-Customer"] = customer_ref(ctx.user)
        if request_id_var.get() not in ("", "-"):
            headers["X-Request-Id"] = request_id_var.get()
        if key:
            headers["Idempotency-Key"] = key
        if ctx.dry_run:
            headers["X-LoopDesk-Dry-Run"] = "1"
        headers.update(self._auth)
        return headers

    def _secrets(self) -> None:
        """Resolved once per call, before anything is sent; never logged, never in the span."""
        self._auth: dict[str, str] = {}
        self._signing: str | None = None
        auth = self.binding.get("auth") or {}
        try:
            if auth.get("secret"):
                value = secrets.resolve(auth["secret"], self.ctx.tenant.slug)
                if auth.get("scheme", "bearer") == "bearer":
                    self._auth["Authorization"] = f"Bearer {value}"
                else:
                    self._auth[auth["header"]] = value
            if self.binding.get("signing_secret"):
                self._signing = secrets.resolve(self.binding["signing_secret"], self.ctx.tenant.slug)
        except secrets.SecretError as exc:
            raise NotSent(f"{self.label}暂时不可用（{exc}）") from None

    def _build(self, client: httpx.Client, args: dict[str, Any], key: str | None, timeout: float) -> httpx.Request:
        method = self.binding.get("method", "GET")
        template = self.binding["url"]
        used = set(_PLACEHOLDER.findall(template))
        url = _PLACEHOLDER.sub(lambda m: quote(str(args.get(m.group(1), "")), safe=""), template)
        rest = {k: v for k, v in args.items() if k not in used}
        params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in rest.items()} if method == "GET" else None
        body = json.dumps(rest, ensure_ascii=False).encode() if method != "GET" else b""
        headers = self._headers(key)
        if body:
            headers["Content-Type"] = "application/json"
        request = client.build_request(method, url, params=params, content=body or None, headers=headers,
                                       timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0)))
        if self._signing:
            secret = self._signing
            stamp = str(int(time.time()))
            message = f"{stamp}\n{method}\n{request.url.raw_path.decode()}\n{key or ''}\n".encode() + body
            request.headers["X-LoopDesk-Timestamp"] = stamp
            request.headers["X-LoopDesk-Signature"] = "v1=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
        return request

    def _send(self, args: dict[str, Any], host: str, key: str | None) -> "_Reply":
        timeout = float(self.binding.get("timeout", 5))
        retry = not self.writes or (key is not None and self.binding.get("idempotency", "header") == "header")
        attempts = int(self.binding.get("max_attempts", 3)) if retry else 1
        entry = record(system=f"tool:{self.row.name}", call=self.row.name, via="http",
                       method=self.binding.get("method", "GET"), path=host)
        client, pin = self.commerce.tool_client(self.ctx.tenant, host)
        stop = deadline()
        started = time.perf_counter()
        status, error, made = None, "", 0
        for made in range(1, attempts + 1):
            left = min(timeout, max(stop - time.monotonic(), 1.0)) if stop is not None else timeout
            request = self._build(client, args, key, left)
            try:
                if pin is not None:
                    request = pin.pin(request)
                response = client.send(request, stream=True)
                try:
                    body = _read_capped(response)
                finally:
                    response.close()
            except EgressError as exc:
                entry.update(attempts=made, error=f"egress: {exc}")
                if made > 1:  # an earlier attempt went out: its outcome is what is unknown
                    raise ToolUnavailable(f"{self.label}所需的业务系统暂时无法访问") from None
                raise NotSent(f"{self.label}暂时不可用（出站策略：{exc}）") from None
            except _TooLarge:
                entry.update(status=response.status_code, attempts=made, error="reply too large")
                if self.writes:
                    raise _Unknown("回复超过 256 KB") from None
                raise ToolUnavailable(f"{self.label}的系统返回了无法使用的结果") from None
            except httpx.RequestError as exc:
                status, error = None, type(exc).__name__
            else:
                status = response.status_code
                if status not in RETRY_STATUSES:
                    entry.update(status=status, attempts=made, latency_ms=int((time.perf_counter() - started) * 1000))
                    return _Reply(status, body, entry)
                error = f"HTTP {status}"
            if made == attempts or (stop is not None and time.monotonic() + BACKOFF_SECONDS * 2 ** (made - 1) >= stop):
                break
            _sleep(BACKOFF_SECONDS * 2 ** (made - 1))
        entry.update(status=status, attempts=made, error=error, latency_ms=int((time.perf_counter() - started) * 1000))
        logger.warning("tool %s for tenant %s: %s after %d attempt(s)", self.row.name, self.ctx.tenant.slug, error, made)
        raise ToolUnavailable(f"{self.label}所需的业务系统暂时无法访问，请稍后再试或转人工客服")

    # ------------------------------------------------------------ the reply
    def _read(self, reply: "_Reply") -> dict[str, Any]:
        data = reply.json()
        if 200 <= reply.status < 300:
            if not isinstance(data, dict):
                reply.call["error"] = "contract: the reply is not a JSON object"
                raise _Unknown("回复不是 JSON 对象") if self.writes else ToolUnavailable(
                    f"{self.label}的系统返回了无法使用的结果")
            return self._shape(data)
        code = str(data.get("code") or "")[:64] if isinstance(data, dict) else ""
        reply.call.update(code=code)
        known = (self.binding.get("errors") or {}).get(code)
        if known is not None or (reply.status in (400, 403, 404) and not self.writes):
            text = known["text"] if known else f"{self.label}没有查到结果"
            output: dict[str, Any] = {"error": text, "code": code or f"http_{reply.status}"}
            if known and known.get("restriction"):
                output.update(restricted=True, reason=text, appeal=appeal_path(self.ctx.tenant))
            raise ToolRefusal(text, output)
        if self.writes and reply.status in (409, 422):
            raise _Unknown(f"HTTP {reply.status} {code or '无错误码'}：不是约定的拒绝码")
        if self.writes and reply.status in (400, 403, 404):  # nothing was done: the request was not accepted
            raise ToolRefusal(f"{self.label}未能办理", {"error": f"{self.label}未能办理", "code": f"http_{reply.status}"})
        if self.writes:
            raise _Unknown(f"HTTP {reply.status}")
        raise ToolUnavailable(f"{self.label}所需的业务系统暂时无法访问，请稍后再试或转人工客服")

    def _shape(self, data: dict[str, Any]) -> dict[str, Any]:
        result = self.binding.get("result") or {}
        fields = result.get("fields")
        output = {k: v for k, v in data.items() if fields is None or k in fields}
        template = result.get("message")
        if template:
            message = render(template, data)
            if message:
                output["message"] = message
        return output


def check_ticket(db: Any, row: ToolExecution, label: str, why: str, customer_id: int) -> None:
    """A person checks a write whose outcome nobody knows (one ticket per ledger row)."""
    if row.ticket_id is not None:
        return
    ticket = Ticket(
        tenant_id=row.tenant_id, conversation_id=row.conversation_id, customer_id=row.customer_id or customer_id,
        kind="issue", priority="high", title=f"待核实：{label}（{row.tool_name}）"[:120],
        description=(f"工具 {row.tool_name} 的请求可能已发出，但没有可确认的结果：{why}。\n"
                     f"幂等键：{row.idempotency_key}。请到业务系统核实是否已办理，再回复顾客。"),
        due_at=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    db.add(ticket)
    db.flush()
    row.ticket_id = ticket.id


def unknown_message(label: str, ticket_id: int | None) -> str:
    return (f"{label}的申请已发出，但业务系统暂时没有返回结果。为避免重复办理，请不要再次提交；"
            f"我们已登记工单 #{ticket_id}，核实后会通知您。")


def appeal_path(tenant: Any) -> str:
    """How a customer appeals a restriction, as the tenant words it (governance.appeal_path)."""
    governance = tenant.governance or {}
    return str(governance.get("appeal_path") or "如对处理结果有异议，可以回复“申请复核”，由人工客服复核。")


class _Unknown(Exception):
    """A write got an answer that says nothing about whether it was done."""


class _TooLarge(Exception):
    pass


class _Reply:
    def __init__(self, status: int, body: bytes, call: dict[str, Any]):
        self.status, self.body, self.call = status, body, call

    def json(self) -> Any:
        """The parsed body; None when it is not JSON (a 2xx without a JSON object breaks the contract)."""
        try:
            return json.loads(self.body) if self.body else None
        except ValueError:
            return None


def _read_capped(response: httpx.Response) -> bytes:
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_REPLY_BYTES:
            raise _TooLarge()
        chunks.append(chunk)
    return b"".join(chunks)
