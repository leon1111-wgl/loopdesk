"""A merchant's own order and payment systems over HTTP, per tenant (LOOPDESK_COMMERCE_APIS).

The contract (paths, payloads, codes) is in docs/DEPLOYMENT.md, "接入你自己的订单和支付系统". What
this side guarantees:
- every request carries the tenant's bearer token, the tenant, the request ID of the conversation
  turn, and, when a signing secret is set, an HMAC-SHA256 signature over timestamp, method, path
  with query string, idempotency key and body;
- a call is sent again after a timeout, a dropped connection or a 408/425/429/5xx only when it is
  idempotent: a read, or a refund request carrying its idempotency key (the same key every time,
  stored in the ledger before the first attempt). Anything else is sent once;
- each phase of an attempt (connecting: at most 3 s, sending, waiting for the reply) waits at most
  ``timeout`` seconds; between attempts come short pauses (0.2 s, 0.4 s, or a Retry-After of up to
  2 s); timeout × max_attempts is at most 20 s (config.CommerceApi), and past the run's deadline
  (base.commerce_deadline) no further attempt starts;
- amounts cross the wire as whole fen, never as floating-point yuan;
- a reply that breaks the contract is an outage (GatewayError), never a crash and never data. On a
  refund, so is a 409 or 422 whose code is not one of the contract's refusals (base.REFUSAL_CODES):
  the refund may exist, so it stays pending and is asked again under the same key.
"""

from __future__ import annotations

import hashlib
import hmac
import json as jsonlib
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ..config import CommerceApi
from ..observability import request_id_var
from .base import (REFUSAL_CODES, GatewayError, OrderInfo, RefundRejected, RefundRequest, RefundResult, ShipmentInfo,
                   deadline, record)

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
BACKOFF_SECONDS = 0.2  # before the first retry; doubled for each one after it
MAX_RETRY_AFTER = 2.0  # a longer Retry-After is not waited for: the customer is waiting
REFUND_STATUSES = frozenset({"processing", "succeeded", "failed"})
_sleep = time.sleep  # tests replace it

logger = logging.getLogger(__name__)


class Reply:
    def __init__(self, response: httpx.Response, call: dict[str, Any]):
        self.response, self.call = response, call

    @property
    def status(self) -> int:
        return self.response.status_code

    def json(self, system: str) -> Any:
        try:
            return self.response.json()
        except ValueError:
            raise _broken(system, self.call, "the reply is not JSON") from None


class CommerceHttp:
    """One tenant's HTTP client: a connection pool, its credentials, the retry rule."""

    def __init__(self, slug: str, conf: CommerceApi, transport: httpx.BaseTransport | None = None):
        self.slug, self.conf = slug, conf
        self.client = httpx.Client(
            base_url=conf.base_url.rstrip("/"), timeout=httpx.Timeout(conf.timeout, connect=min(conf.timeout, 3.0)),
            transport=transport,
            follow_redirects=False,  # a redirect would carry the token somewhere else
            headers={"Authorization": f"Bearer {conf.token}", "Accept": "application/json",
                     "User-Agent": "LoopDesk/1.0", "X-LoopDesk-Tenant": slug},
        )

    def request(self, method: str, path: str, *, system: str = "orders", call: str = "", json: Any = None,
                params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                idempotency_key: str | None = None) -> Reply:
        """One call, with attempts as the module docstring says. Returns the reply once one arrives
        that is not worth retrying (a 4xx included, for the caller to read); raises GatewayError when
        none does."""
        idempotent = method == "GET" or idempotency_key is not None
        attempts = self.conf.max_attempts if idempotent else 1
        body = jsonlib.dumps(json, ensure_ascii=False).encode() if json is not None else b""
        extra = dict(headers or {})
        if body:
            extra["Content-Type"] = "application/json"
        if idempotency_key is not None:
            extra["Idempotency-Key"] = idempotency_key
        if request_id_var.get() not in ("", "-"):  # "-": not inside a request (a job, the MCP server)
            extra["X-Request-Id"] = request_id_var.get()
        started = time.perf_counter()
        entry = record(system=system, call=call or path, via="http", method=method, path=path)
        unsendable = next((k for k, v in extra.items() if not v.isascii() or "\r" in v or "\n" in v), None)
        if unsendable is not None:  # never sent: a header cannot carry it (or it would inject another one)
            entry.update(attempts=0, error=f"{unsendable} cannot be sent in a header")
            raise GatewayError(system, f"{call or path}: {unsendable} cannot be sent in a header", attempts=0)
        stop = deadline()
        status: int | None = None
        error = ""
        made = 0
        for made in range(1, attempts + 1):
            # An attempt starting near the deadline waits only for what is left of it (at least a second).
            left = min(self.conf.timeout, max(stop - time.monotonic(), 1.0)) if stop is not None else None
            wait = httpx.Timeout(left, connect=min(left, 3.0)) if left is not None else httpx.USE_CLIENT_DEFAULT
            request = self.client.build_request(method, path, content=body or None, params=params, headers=extra,
                                                timeout=wait)
            self._sign(request, body, idempotency_key)
            response = None
            try:
                response = self.client.send(request)
            except httpx.RequestError as exc:  # timeouts, refused or dropped connections, an unreadable body
                status, error = None, type(exc).__name__
            else:
                status = response.status_code
                if status not in RETRY_STATUSES:
                    entry.update(status=status, attempts=made, latency_ms=_ms(started))
                    return Reply(response, entry)
                error = f"HTTP {status}"
            pause = self._pause(made, response)
            if made == attempts or (stop is not None and time.monotonic() + pause >= stop):
                break  # the customer has waited long enough: what is left is the reconciliation's
            _sleep(pause)
        entry.update(status=status, attempts=made, latency_ms=_ms(started), error=error)
        logger.warning("%s %s %s for tenant %s: %s after %d attempt(s)", system, method, path, self.slug, error, made)
        raise GatewayError(system, f"{call or path}: {error} after {made} attempt(s)", status=status, attempts=made)

    def _sign(self, request: httpx.Request, body: bytes, idempotency_key: str | None) -> None:
        if not self.conf.signing_secret:
            return
        timestamp = str(int(time.time()))  # per attempt: a retry minutes later is not refused as stale
        message = (f"{timestamp}\n{request.method}\n{request.url.raw_path.decode()}\n{idempotency_key or ''}\n"
                   .encode() + body)
        digest = hmac.new(self.conf.signing_secret.encode(), message, hashlib.sha256).hexdigest()
        request.headers["X-LoopDesk-Timestamp"] = timestamp
        request.headers["X-LoopDesk-Signature"] = f"v1={digest}"

    @staticmethod
    def _pause(attempt: int, response: httpx.Response | None) -> float:
        retry_after = response.headers.get("retry-after") if response is not None else None
        try:
            if retry_after is not None:
                return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER)
        except ValueError:  # an HTTP date: not worth waiting for here
            pass
        return BACKOFF_SECONDS * 2 ** (attempt - 1)

    def close(self) -> None:
        self.client.close()


class HttpOrderGateway:
    via = "http"

    def __init__(self, api: CommerceHttp):
        self.api = api

    def get_order(self, order_id: str) -> OrderInfo | None:
        reply = self.api.request("GET", f"/orders/{_segment(order_id)}", call="get_order")
        if reply.status == 404:
            return None
        order = _order(_ok(reply, "orders").json("orders"), reply.call)
        if order.order_id != order_id:
            raise _broken("orders", reply.call, f"asked for {order_id}, got {order.order_id}")
        return order

    def list_orders(self, customer: str, limit: int = 20) -> list[OrderInfo]:
        # The customer goes in a header: URLs end up in access logs, and this one is an e-mail address.
        reply = self.api.request("GET", "/orders", call="list_orders", params={"limit": limit},
                                 headers={"X-LoopDesk-Customer": customer})
        data = _ok(reply, "orders").json("orders")
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            raise _broken("orders", reply.call, "'orders' must be a list")
        orders = [_order(item, reply.call) for item in data["orders"]]
        if any(o.customer_ref != customer for o in orders):  # never show one customer another's orders
            raise _broken("orders", reply.call, "an order of another customer was listed")
        return orders[:limit]

    def get_shipment(self, order_id: str) -> ShipmentInfo | None:
        reply = self.api.request("GET", f"/orders/{_segment(order_id)}/shipment", call="get_shipment")
        if reply.status == 404:
            return None
        data = _ok(reply, "orders").json("orders")
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list) or not all(
                isinstance(e, dict) and isinstance(e.get("time"), str) and isinstance(e.get("desc"), str) for e in events):
            raise _broken("orders", reply.call, "'events' must be a list of {time, desc}")
        return ShipmentInfo(carrier=_text(data, "carrier", reply.call), tracking_no=_text(data, "tracking_no", reply.call),
                            events=[{"time": e["time"], "desc": e["desc"]} for e in events])


class HttpRefundGateway:
    via = "http"

    def __init__(self, api: CommerceHttp):
        self.api = api

    def create_refund(self, request: RefundRequest) -> RefundResult:
        reply = self.api.request("POST", "/refunds", system="refunds", call="create_refund",
                                 idempotency_key=request.idempotency_key, json={
                                     "order_id": request.order_id, "amount_fen": _to_fen(request.amount),
                                     "reason": request.reason, "approval_id": request.approval_id,
                                     "loopdesk_refund_id": request.loopdesk_refund_id,
                                 })
        if reply.status in (409, 422):
            data = reply.json("refunds")
            code = str(data.get("code") or "")[:64] if isinstance(data, dict) else ""
            detail = str(data.get("message") or "")[:200] if isinstance(data, dict) else ""
            reply.call.update(code=code, detail=detail)
            if code in REFUSAL_CODES:  # refused: nothing was paid
                raise RefundRejected(code, detail)
            # Anything else (idempotency_key_in_use: the first request under this key is still being
            # made; a "try later") says nothing about whether the refund exists: it stays pending.
            reply.call["error"] = f"HTTP {reply.status} {code or 'without a code'}: not a refusal"
            logger.warning("refund %s: HTTP %s %s is not a refusal; kept pending", request.loopdesk_refund_id,
                           reply.status, code)
            raise GatewayError("refunds", f"create_refund: HTTP {reply.status} {code}: not a refusal",
                               status=reply.status, attempts=reply.call.get("attempts", 1))
        return _refund(_ok(reply, "refunds").json("refunds"), reply.call)

    def get_refund(self, refund_id: str) -> RefundResult:
        reply = self.api.request("GET", f"/refunds/{_segment(refund_id)}", system="refunds", call="get_refund")
        result = _refund(_ok(reply, "refunds").json("refunds"), reply.call)
        if result.refund_id != refund_id:
            raise _broken("refunds", reply.call, f"asked for {refund_id}, got {result.refund_id}")
        return result


# ---------------------------------------------------------------- reading replies
def _ok(reply: Reply, system: str) -> Reply:
    """A 2xx, or a GatewayError: a 401/403 is a configuration problem, a 404 here a wrong URL, a 400
    a request the other side cannot read. None of them is retried."""
    if 200 <= reply.status < 300:
        return reply
    reply.call["error"] = f"HTTP {reply.status}"
    logger.error("%s call %s answered HTTP %s", system, reply.call.get("call"), reply.status)
    raise GatewayError(system, f"{reply.call.get('call')}: HTTP {reply.status}", status=reply.status,
                       attempts=reply.call.get("attempts", 1))


def _broken(system: str, call: dict[str, Any], why: str) -> GatewayError:
    call["error"] = f"contract: {why}"
    logger.error("%s call %s broke the contract: %s", system, call.get("call"), why)
    return GatewayError(system, f"{call.get('call')}: contract: {why}", status=call.get("status"),
                        attempts=call.get("attempts", 1))


def _segment(value: str) -> str:
    return quote(value, safe="")


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _text(data: dict[str, Any], key: str, call: dict[str, Any], required: bool = True) -> str:
    value = data.get(key)
    if isinstance(value, str) and (value or not required):
        return value
    raise _broken("orders", call, f"{key!r} must be a non-empty string")


def _yuan(data: dict[str, Any], key: str, call: dict[str, Any]) -> float:
    """A whole, non-negative number of fen, as yuan (what the rest of LoopDesk works in)."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _broken("orders", call, f"{key!r} must be a whole number of fen, at least 0")
    return value / 100


def _to_fen(yuan: float) -> int:
    return int(round(yuan * 100))


def _time(data: dict[str, Any], key: str, call: dict[str, Any]) -> datetime | None:
    value = data.get(key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise _broken("orders", call, f"{key!r} must be an ISO 8601 time") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _order(data: Any, call: dict[str, Any]) -> OrderInfo:
    if not isinstance(data, dict):
        raise _broken("orders", call, "an order must be an object")
    items = data.get("items", [])
    if not isinstance(items, list) or not all(
            isinstance(i, dict) and isinstance(i.get("name"), str) and isinstance(i.get("qty"), int)
            and not isinstance(i.get("qty"), bool) for i in items):
        raise _broken("orders", call, "'items' must be a list of {name, qty, price_fen}")
    return OrderInfo(
        order_id=_text(data, "order_id", call), customer_ref=_text(data, "customer_ref", call).strip().lower(),
        status=_text(data, "status", call), amount=_yuan(data, "amount_fen", call),
        items=[{"name": i["name"], "qty": i["qty"], "price": _yuan(i, "price_fen", call)} for i in items],
        paid_at=_time(data, "paid_at", call), delivered_at=_time(data, "delivered_at", call),
        region=data.get("region") if isinstance(data.get("region"), str) else "domestic",
    )


def _refund(data: Any, call: dict[str, Any]) -> RefundResult:
    if not isinstance(data, dict) or not isinstance(data.get("refund_id"), str) or not data["refund_id"]:
        raise _broken("refunds", call, "'refund_id' must be a non-empty string")
    if data.get("status") not in REFUND_STATUSES:
        raise _broken("refunds", call, f"'status' must be one of {', '.join(sorted(REFUND_STATUSES))}")
    return RefundResult(refund_id=data["refund_id"][:128], status=data["status"])
