"""The demo's order and payment systems: the orders table, in LoopDesk's own database.

The default for every tenant without LOOPDESK_COMMERCE_APIS, and always for the eval sandbox (its
fixtures are the orders the eval cases ask about). Calls run in the caller's session and
transaction, so a refund's change to the order commits with the ledger row that records it.
"""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Order, Refund, User
from .base import (REFUNDABLE, OrderInfo, RefundRejected, RefundRequest, RefundResult, ShipmentInfo, customer_ref,
                   record)


class LocalOrderGateway:
    via = "local"

    def __init__(self, db: Session, tenant_id: int):
        self.db, self.tenant_id = db, tenant_id

    def get_order(self, order_id: str) -> OrderInfo | None:
        record(system="orders", call="get_order", via=self.via)
        order = self.db.get(Order, order_id)
        return self._info(order) if order is not None and order.tenant_id == self.tenant_id else None

    def list_orders(self, customer: str, limit: int = 20) -> list[OrderInfo]:
        record(system="orders", call="list_orders", via=self.via)
        orders = self.db.execute(
            select(Order).join(User, User.id == Order.customer_id)
            .where(Order.tenant_id == self.tenant_id, func.lower(User.email) == customer)
            .order_by(Order.paid_at.desc()).limit(limit)
        ).scalars().all()
        return [self._info(o) for o in orders]

    def get_shipment(self, order_id: str) -> ShipmentInfo | None:
        record(system="orders", call="get_shipment", via=self.via)
        order = self.db.get(Order, order_id)
        shipment = (order.shipment or {}) if order is not None and order.tenant_id == self.tenant_id else {}
        if not shipment.get("events"):
            return None
        return ShipmentInfo(carrier=shipment.get("carrier") or "", tracking_no=shipment.get("tracking_no") or "",
                            events=list(shipment["events"]))

    def _info(self, order: Order) -> OrderInfo:
        return OrderInfo(
            order_id=order.id, customer_ref=customer_ref(self.db.get(User, order.customer_id)), status=order.status,
            amount=order.amount, items=list(order.items or []), paid_at=order.paid_at,
            delivered_at=order.delivered_at, region=order.region,
        )


class LocalRefundGateway:
    """Making a refund moves the order to "refunding", if it can still be refunded: one conditional
    UPDATE, so an order the back office changed meanwhile is refused instead of refunded. Money does
    not move in the demo; the ledger row stays "processing"."""

    via = "local"

    def __init__(self, db: Session, tenant_id: int):
        self.db, self.tenant_id = db, tenant_id

    def create_refund(self, request: RefundRequest) -> RefundResult:
        call = record(system="refunds", call="create_refund", via=self.via)
        claimed = self.db.execute(
            update(Order).where(Order.id == request.order_id, Order.tenant_id == self.tenant_id,
                                Order.status.in_(REFUNDABLE))
            .values(status="refunding").execution_options(synchronize_session="fetch")
        ).rowcount
        if claimed != 1:
            call["code"] = "order_not_refundable"
            raise RefundRejected("order_not_refundable", "the order is not in a refundable state")
        return RefundResult(refund_id=f"local-{request.loopdesk_refund_id}", status="processing")

    def get_refund(self, refund_id: str) -> RefundResult:
        record(system="refunds", call="get_refund", via=self.via)
        refund = self.db.execute(select(Refund).where(Refund.external_id == refund_id,
                                                      Refund.tenant_id == self.tenant_id)).scalar_one()
        return RefundResult(refund_id=refund_id, status=refund.status)
