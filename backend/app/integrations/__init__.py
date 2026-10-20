"""Gateways to the order and payment systems the agent's tools act on (N-028).

    Commerce(settings).orders(tenant, db)    # OrderGateway: get_order, list_orders, get_shipment
    Commerce(settings).refunds(tenant, db)   # RefundGateway: create_refund (idempotency key), get_refund

A tenant listed in LOOPDESK_COMMERCE_APIS uses its own systems over HTTP (http.py); every other
tenant, and the eval sandbox always, uses the demo tables (local.py). See base.py for the types.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from .base import (REFUNDABLE, REFUSAL_CODES, GatewayError, OrderGateway, OrderInfo, RefundGateway, RefundRejected,
                   RefundRequest, RefundResult, ShipmentInfo, commerce_deadline, customer_ref, reads, record,
                   recording)
from .http import CommerceHttp, HttpOrderGateway, HttpRefundGateway
from .local import LocalOrderGateway, LocalRefundGateway

__all__ = [
    "REFUNDABLE", "REFUSAL_CODES", "Commerce", "GatewayError", "OrderGateway", "OrderInfo", "RefundGateway",
    "RefundRejected", "RefundRequest", "RefundResult", "ShipmentInfo", "commerce_deadline", "customer_ref", "reads",
    "record", "recording",
]


class Commerce:
    """Which gateways serve which tenant. One per process: the HTTP clients (connection pools) are
    shared; the local gateways work in the caller's database session, so they are made per call."""

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        apis = settings.commerce_apis if settings is not None else {}
        self.apis = {slug: CommerceHttp(slug, conf, transport) for slug, conf in apis.items()}

    @classmethod
    def local(cls) -> "Commerce":
        """The demo tables for every tenant: what the eval sandbox answers from."""
        return cls()

    def api(self, slug: str) -> CommerceHttp:
        return self.apis[slug]

    def via(self, tenant: Any) -> str:
        return "http" if tenant.slug in self.apis else "local"

    def orders(self, tenant: Any, db: Session | None) -> OrderGateway:
        api = self.apis.get(tenant.slug)
        return HttpOrderGateway(api) if api is not None else LocalOrderGateway(db, tenant.id)

    def refunds(self, tenant: Any, db: Session | None) -> RefundGateway:
        api = self.apis.get(tenant.slug)
        return HttpRefundGateway(api) if api is not None else LocalRefundGateway(db, tenant.id)

    def close(self) -> None:
        for api in self.apis.values():
            api.close()
