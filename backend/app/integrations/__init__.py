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
    shared; the local gateways work in the caller's database session, so they are made per call.

    It also routes the tenant-declared tools (app/tooling): a host of a synthetic merchant (the demo
    tenants' ".example" back offices, app/mockmerchant.py) is answered in process, in demo mode and
    always in the sandbox; any other host goes over the network to the address the egress policy
    checked. ``Commerce.local()`` is the sandbox: tenant tools reach synthetic merchants only, each
    starting from its pack's records."""

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None,
                 tool_transport: httpx.BaseTransport | None = None):
        apis = settings.commerce_apis if settings is not None else {}
        self.apis = {slug: CommerceHttp(slug, conf, transport) for slug, conf in apis.items()}
        self.sandboxed = settings is None
        self.synthetic_enabled = settings is None or bool(settings.seed_demo)
        self.tool_transport = tool_transport  # tests: every tenant tool request that is not synthetic goes here
        self._merchants = None
        self._tool_clients: dict[tuple[str, str], tuple[httpx.Client, Any]] = {}

    @classmethod
    def local(cls) -> "Commerce":
        """The demo tables for every tenant: what the eval sandbox answers from."""
        return cls()

    @property
    def merchants(self):
        if self._merchants is None:
            from ..mockmerchant import SyntheticMerchants

            self._merchants = SyntheticMerchants()
        return self._merchants

    def synthetic_host(self, tenant: Any, host: str) -> bool:
        return self.synthetic_enabled and host.endswith(".example") and self.merchants.serves(tenant.slug, host)

    def tool_client(self, tenant: Any, host: str) -> tuple[httpx.Client, Any]:
        """(client, egress policy to pin the request with, or None when it never leaves the process).
        One client per tenant and host: a pinned connection must never carry another host's request."""
        key = (tenant.slug, host)
        if key not in self._tool_clients:
            from ..tooling.egress import EgressPolicy

            if self.synthetic_host(tenant, host):
                transport, pin = self.merchants.transport(tenant.slug), None
            elif self.tool_transport is not None:
                transport, pin = self.tool_transport, None
            else:
                transport, pin = None, EgressPolicy(tenant.tool_hosts or ())
            self._tool_clients[key] = (httpx.Client(transport=transport, follow_redirects=False), pin)
        client, pin = self._tool_clients[key]
        if pin is not None:  # the allowlist may have changed since the client was made
            from ..tooling.egress import EgressPolicy

            pin = EgressPolicy(tenant.tool_hosts or ())
        return client, pin

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
        for client, _ in self._tool_clients.values():
            client.close()
