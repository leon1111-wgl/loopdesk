"""A merchant's order and payment systems, in process, speaking LoopDesk's HTTP contract
(docs/DEPLOYMENT.md, "接入你自己的订单和支付系统"). Tests reach it through httpx.MockTransport:
no socket, no network.

It behaves the way the contract asks a real one to: amounts are integers in fen; a refund request
carries an Idempotency-Key, and the same key gets the same answer (the second refund is never
made), or a 409 idempotency_key_in_use while the first request under it is still being made; a
refund for more than is left to refund, or for an order that cannot be refunded, is refused with a
contract code. Faults are injected per request: an HTTP status (with a code), or a transport error
raised before the request is handled or after it (the refund was made, the reply was lost).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

TOKEN = "test-commerce-token-0123456789abcdef"
REFUNDABLE = {"paid", "shipped", "delivered"}


@dataclass
class Fault:
    method: str
    path: str  # prefix
    status: int | None = None
    error: type[httpx.TransportError] | None = None
    times: int = 1
    after: bool = False  # raise the error once the request has been handled
    code: str = "unavailable"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def fen(yuan: float) -> int:
    return round(yuan * 100)


class FakeCommerce:
    def __init__(self, token: str = TOKEN, signing_secret: str | None = None):
        self.token, self.signing_secret = token, signing_secret
        self.orders: dict[str, dict[str, Any]] = {}
        self.shipments: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, tuple[str, int, dict[str, Any]]] = {}  # key -> (fingerprint, status, body)
        self.in_flight: set[str] = set()  # keys whose first request is still being made
        self.refusals: dict[str, tuple[int, str, str]] = {}  # order id -> (status, code, message)
        self.new_refund_status = "processing"
        self.keep_delivered = False  # a partial refund leaves the order "delivered" (some merchants do)
        self.requests: list[httpx.Request] = []
        self.faults: list[Fault] = []
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ test setup
    def add_order(self, order_id: str, customer: str, amount: float, *, status: str = "delivered",
                  delivered_days_ago: float | None = 2, items: list[dict] | None = None,
                  shipment: dict[str, Any] | None = None) -> None:
        now = datetime.now(timezone.utc)
        delivered = now - timedelta(days=delivered_days_ago) if delivered_days_ago is not None else None
        items = items or [{"name": "远程测试商品", "qty": 1, "price": amount}]
        self.orders[order_id] = {
            "order_id": order_id, "customer_ref": customer, "status": status, "amount_fen": fen(amount),
            "items": [{"name": i["name"], "qty": i["qty"], "price_fen": fen(i["price"])} for i in items],
            "paid_at": _iso(now - timedelta(days=(delivered_days_ago or 0) + 2)),
            "delivered_at": _iso(delivered), "region": "domestic", "refunded_fen": 0,
        }
        if shipment is not None:
            self.shipments[order_id] = shipment

    def fail(self, method: str, path: str, *, status: int | None = None, code: str = "unavailable",
             error: type[httpx.TransportError] | None = None, times: int = 1, after: bool = False) -> None:
        self.faults.append(Fault(method, path, status, error, times, after, code))

    def refuse(self, order_id: str, status: int, code: str, message: str) -> None:
        self.refusals[order_id] = (status, code, message)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.startswith(path)]

    def refunded(self, order_id: str) -> float:
        return self.orders[order_id]["refunded_fen"] / 100

    # ------------------------------------------------------------------ the server
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fault = next((f for f in self.faults if f.times > 0 and f.method == request.method
                      and request.url.path.startswith(f.path)), None)
        if fault is not None:
            fault.times -= 1
            if not fault.after:
                if fault.error is not None:
                    raise fault.error("injected", request=request)
                return httpx.Response(fault.status or 503, json={"code": fault.code, "message": "injected"})
        response = self._route(request)
        if fault is not None and fault.after:
            raise (fault.error or httpx.ReadTimeout)("reply lost", request=request)
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"code": "unauthorized", "message": "bad token"})
        if self.signing_secret is not None and not self._signed(request):
            return httpx.Response(401, json={"code": "bad_signature", "message": "signature mismatch"})
        parts = [p for p in request.url.path.split("/") if p][1:]  # drop the base path ("loopdesk")
        if request.method == "GET" and parts == ["orders"]:
            customer = request.headers.get("x-loopdesk-customer", "")
            mine = [self._order_out(o) for o in self.orders.values() if o["customer_ref"] == customer]
            return httpx.Response(200, json={"orders": mine[: int(request.url.params.get("limit", 20))]})
        if request.method == "GET" and len(parts) == 2 and parts[0] == "orders":
            order = self.orders.get(parts[1])
            return httpx.Response(200, json=self._order_out(order)) if order else self._not_found()
        if request.method == "GET" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "shipment":
            shipment = self.shipments.get(parts[1])
            return httpx.Response(200, json=shipment) if shipment else self._not_found()
        if request.method == "POST" and parts == ["refunds"]:
            return self._create_refund(request)
        if request.method == "GET" and len(parts) == 2 and parts[0] == "refunds":
            refund = self.refunds.get(parts[1])
            return httpx.Response(200, json=self._refund_out(refund)) if refund else self._not_found()
        return self._not_found()

    def _create_refund(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("idempotency-key")
        if not key:
            return httpx.Response(400, json={"code": "idempotency_key_required", "message": "missing key"})
        if key in self.in_flight:
            return httpx.Response(409, json={"code": "idempotency_key_in_use", "message": "request in progress"})
        body = json.loads(request.content)
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if key in self.by_key:  # the same request again: the same answer, nothing new happens
            seen, status, out = self.by_key[key]
            if seen != fingerprint:
                return httpx.Response(422, json={"code": "idempotency_key_reused", "message": "different request"})
            return httpx.Response(status, json=out)
        status, out = self._refund_once(body)
        self.by_key[key] = (fingerprint, status, out)
        return httpx.Response(status, json=out)

    def _refund_once(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        order = self.orders.get(body["order_id"])
        if order is None:
            return 422, {"code": "order_not_found", "message": "no such order"}
        if body["order_id"] in self.refusals:
            status, code, message = self.refusals[body["order_id"]]
            return status, {"code": code, "message": message}
        if order["status"] not in REFUNDABLE:
            return 409, {"code": "order_not_refundable", "message": f"order is {order['status']}"}
        if body["amount_fen"] > order["amount_fen"] - order["refunded_fen"]:
            return 422, {"code": "amount_exceeds_refundable", "message": "over the refundable balance"}
        refund_id = f"RF{len(self.refunds) + 1:06d}"
        self.refunds[refund_id] = {"refund_id": refund_id, "order_id": order["order_id"],
                                   "amount_fen": body["amount_fen"], "status": self.new_refund_status, "body": body}
        order["refunded_fen"] += body["amount_fen"]
        if not self.keep_delivered:
            order["status"] = "refunding"
        return 201, self._refund_out(self.refunds[refund_id])

    def _signed(self, request: httpx.Request) -> bool:
        timestamp = request.headers.get("x-loopdesk-timestamp", "")
        key = request.headers.get("idempotency-key", "")
        path = request.url.raw_path.decode()
        message = f"{timestamp}\n{request.method}\n{path}\n{key}\n".encode() + request.content
        expected = "v1=" + hmac.new(self.signing_secret.encode(), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(request.headers.get("x-loopdesk-signature", ""), expected)

    @staticmethod
    def _order_out(order: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in order.items() if k != "refunded_fen"}

    @staticmethod
    def _refund_out(refund: dict[str, Any]) -> dict[str, Any]:
        return {k: refund[k] for k in ("refund_id", "order_id", "amount_fen", "status")}

    @staticmethod
    def _not_found() -> httpx.Response:
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})
