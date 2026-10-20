"""What the agent's tools need from the systems they act on, whoever runs them.

LoopDesk is the system of record for neither orders nor money. The order system knows the orders and
their shipments; the payment system makes refunds. The tools ask them through two gateways and keep
only their own record of what they did: the refund ledger (models.Refund), which the velocity limit,
the per-order claim and approval idempotency are built on, wherever the order lives.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# Order states the contract knows. Any other state is shown as the order system names it, and is
# not refundable.
REFUNDABLE = frozenset({"paid", "shipped", "delivered"})
# The codes with which a payment system says "refused, nothing was paid". A 409 or 422 with any other
# code (idempotency_key_in_use while the first request under the key is still being made, a "try
# later") is not a refusal: the refund may exist, so it stays pending and is asked again.
REFUSAL_CODES = frozenset({"order_not_refundable", "amount_exceeds_refundable", "order_not_found",
                           "refund_declined"})


def customer_ref(user: Any) -> str:
    """How LoopDesk names a customer to the merchant's systems: the e-mail address they sign in with,
    the identity both sides have. A merchant that keys customers by phone or member number maps it in
    the service that implements the contract."""
    return user.email.strip().lower()


@dataclass(frozen=True)
class OrderInfo:
    order_id: str
    customer_ref: str  # the customer who placed it (customer_ref)
    status: str
    amount: float  # what was paid
    items: list[dict[str, Any]]  # {"name", "qty", "price"}
    paid_at: datetime | None
    delivered_at: datetime | None  # None until delivered; after-sales windows count from it
    region: str = "domestic"


@dataclass(frozen=True)
class ShipmentInfo:
    carrier: str
    tracking_no: str
    events: list[dict[str, str]] = field(default_factory=list)  # {"time", "desc"}, oldest first


@dataclass(frozen=True)
class RefundRequest:
    """One refund, built from columns of its ledger row that never change: sent again (a retry, the
    reconciliation job), it is the same request under the same key, so the payment system makes it at
    most once. Nothing from the customer's account goes in: an e-mail change or an erasure between two
    sends would make the same key carry a different request."""

    idempotency_key: str
    order_id: str
    amount: float  # yuan; on the wire, whole fen
    reason: str
    approval_id: int | None
    loopdesk_refund_id: int  # the ledger row, for the merchant's own reconciliation


@dataclass(frozen=True)
class RefundResult:
    refund_id: str  # the payment system's id
    status: str  # processing | succeeded | failed


class GatewayError(Exception):
    """No usable answer: unreachable, timed out, an error status, or a reply that breaks the contract.
    For a refund request that means its outcome is unknown: it may have been made."""

    def __init__(self, system: str, detail: str, *, status: int | None = None, attempts: int = 1):
        super().__init__(detail)
        self.system, self.detail, self.status, self.attempts = system, detail, status, attempts


class RefundRejected(Exception):
    """The payment system answered and refused: nothing was paid. ``code`` is from the contract;
    the system's own message may name internal rules, so it goes to the trace, not to the customer."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class OrderGateway(Protocol):
    via: str  # local | http

    def get_order(self, order_id: str) -> OrderInfo | None:
        """The order, or None if the order system does not know it (for this tenant)."""

    def list_orders(self, customer: str, limit: int = 20) -> list[OrderInfo]:
        """One customer's orders (by customer_ref), newest first."""

    def get_shipment(self, order_id: str) -> ShipmentInfo | None:
        """The shipment of an order, or None if it has not been shipped."""


class RefundGateway(Protocol):
    via: str

    def create_refund(self, request: RefundRequest) -> RefundResult:
        """Make the refund, or return the one already made under this idempotency key. Raises
        RefundRejected when refused, GatewayError when there is no answer."""

    def get_refund(self, refund_id: str) -> RefundResult:
        """Where a refund the payment system accepted stands now."""


# Per tool call (AgentRuntime._run_tool), in context variables so the gateways need no tracer:
# - the calls made, for the tool's trace span;
# - what was read, so the tool's precheck, policy and handler see one snapshot of an order (one read,
#   not three; a resumed approval is a new tool call and reads afresh);
# - the run's deadline: past it a call makes no further attempts.
_calls: ContextVar[list[dict[str, Any]] | None] = ContextVar("integration_calls", default=None)
_reads: ContextVar[dict[Any, Any] | None] = ContextVar("integration_reads", default=None)
_deadline: ContextVar[float | None] = ContextVar("integration_deadline", default=None)


@contextmanager
def recording() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    tokens = (_calls.set(calls), _reads.set({}))
    try:
        yield calls
    finally:
        _calls.reset(tokens[0])
        _reads.reset(tokens[1])


def reads() -> dict[Any, Any] | None:
    """This tool call's reads, or None outside a tool call (nothing is kept)."""
    return _reads.get()


@contextmanager
def commerce_deadline(deadline: float | None) -> Iterator[None]:
    """A time.monotonic() deadline for the calls made inside (the run's); None for none."""
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def deadline() -> float | None:
    return _deadline.get()


def record(**call: Any) -> dict[str, Any]:
    """Note one gateway call (no headers: they carry the token). Returned so the caller can add
    what it learns from the reply, such as a refusal's code."""
    calls = _calls.get()
    if calls is not None:
        calls.append(call)
    return call
