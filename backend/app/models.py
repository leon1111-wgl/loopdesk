"""ORM models.

Every business row carries ``tenant_id``; every query in the API layer filters on it.
That single rule is what makes the platform multi-tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    industry: Mapped[str] = mapped_column(String(64), default="ecommerce")
    auto_refund_limit: Mapped[float] = mapped_column(Float, default=50.0)
    ticket_sla_hours: Mapped[int] = mapped_column(Integer, default=24)
    # After-sales windows in days since delivery: no-reason returns, then quality problems only.
    return_window_days: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    quality_window_days: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    # Refunds above this amount are approved by an administrator only (agents may still reject).
    approval_admin_threshold: Mapped[float] = mapped_column(Float, default=500.0, server_default="500")
    # IANA name; dates shown to people (dashboard days, order dates, exports) are this zone's.
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", server_default="Asia/Shanghai")
    # Official channels: the sites answers may send customers to and the contact accounts that are
    # really the tenant's. A link or an account anywhere else is held back in knowledge that asks
    # for something, and stops an answer (guardrails.injection.Channels).
    official_domains: Mapped[list] = mapped_column(JSON, default=list)
    official_contacts: Mapped[list] = mapped_column(JSON, default=list)
    # Bumped in the same transaction as every change to the tenant's chunks: the retrieval index
    # and the answer cache compare it instead of counting the chunks on every search.
    kb_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    audit_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # last audit entry's number
    # Script customers read (zh-Hans | zh-Hant-HK | zh-Hant-TW): answers are converted at the edge
    # (app/governance/script.py); stored text stays simplified, what knowledge and checks work in.
    locale: Mapped[str] = mapped_column(String(16), default="zh-Hans", server_default="zh-Hans")
    currency: Mapped[str] = mapped_column(String(3), default="CNY", server_default="CNY")  # ISO 4217
    # Where this tenant's personal data may be processed (e.g. "GBA"), and the regions that counts
    # as: tools and MCP servers declare a region, audit entries carry the tag (app/governance/residency.py).
    data_residency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    allowed_regions: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # Hosts the tenant's own tools and MCP servers may call. Set by the platform operator (tenant
    # pack), read-only through the tenant API: an admin session cannot point a tool elsewhere.
    tool_hosts: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # Disclosure texts per channel, the appeal path, the review SLA, suggestions (app/governance).
    governance: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))  # admin | agent | customer
    password_hash: Mapped[str] = mapped_column(String(255))
    # Carried in every token; bumping it (erasure) invalidates all tokens issued before.
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Set when staff leave (N-102): the account can no longer sign in or be assigned work, but
    # keeps its name, so approvals, tickets and the audit log still say who did what.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(255), default="")
    visibility: Mapped[str] = mapped_column(String(16), default="public")  # public | internal
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    flagged_chunks: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(String(512), default="")
    text: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    flagged_injection: Mapped[bool] = mapped_column(Boolean, default=False)  # held back from retrieval
    # Why the scan held it back (guardrails.injection rule names), and who released it after review.
    flag_reasons: Mapped[list] = mapped_column(JSON, default=list)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


CHANNEL_CHAT = "external_id IS NOT NULL"


class Conversation(Base):
    __tablename__ = "conversations"
    # One conversation per external chat and user (a Feishu chat): two first messages arriving
    # together both pass a check-then-insert, and a second row would make every later lookup
    # of that chat ambiguous.
    __table_args__ = (
        Index("uq_conversations_channel_chat", "tenant_id", "channel", "external_id", "user_id", unique=True,
              sqlite_where=text(CHANNEL_CHAT), postgresql_where=text(CHANNEL_CHAT)),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="新对话")
    channel: Mapped[str] = mapped_column(String(16), default="web")  # web | feishu | eval | api
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="bot")  # bot | handoff
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | agent (a person) | system (notice)
    content: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    """One agent execution = one trace."""

    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(24), default="running")
    # running | waiting_approval | completed | handoff | failed
    model: Mapped[str] = mapped_column(String(128), default="")
    route_tier: Mapped[str] = mapped_column(String(16), default="")
    input_text: Mapped[str] = mapped_column(Text, default="")
    output_text: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    evidence_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)  # pass | warn | blocked
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # serialized agent state for HITL resume
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_eval: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Span(Base):
    __tablename__ = "spans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # router | llm | tool | retrieval | guardrail | verify | approval | notify
    name: Mapped[str] = mapped_column(String(128))
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_call_id: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected | withdrawn (by the customer, N-127)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    # Who may approve, when the tool's own policy says (a tenant tool); NULL: the refund rule by amount.
    approver_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Order(Base):
    """Stands in for the merchant's order system that the agent integrates with."""

    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    items: Mapped[list] = mapped_column(JSON, default=list)
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))  # paid | shipped | delivered | refunding | refunded
    region: Mapped[str] = mapped_column(String(16), default="domestic")
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipment: Mapped[dict] = mapped_column(JSON, default=dict)


# A refund that holds its order: filed (keyed) and not refused. Rows from before the ledger have no
# key; their orders were already held by the order's own status.
LIVE_REFUND = "idempotency_key IS NOT NULL AND status <> 'failed'"


class Refund(Base):
    """LoopDesk's ledger of the refunds its agent filed, wherever the order lives (the demo tables or
    the merchant's own systems, app/integrations). The velocity limit counts it, approvals look their
    refund up in it, and it claims the order: at most one live refund per order, enforced by the
    database. A row is written (pending) before the payment system is asked, under the idempotency
    key every later attempt reuses.

    status: claiming (only inside the transaction that writes it) | pending (filed, no answer yet:
    it may have been made) | processing | succeeded | failed (refused: nothing was paid)."""

    __tablename__ = "refunds"
    __table_args__ = (
        Index("uq_refunds_live_order", "tenant_id", "order_id", unique=True,
              sqlite_where=text(LIVE_REFUND), postgresql_where=text(LIVE_REFUND)),
        Index("uq_refunds_idempotency_key", "idempotency_key", unique=True),
        # The refund console: a tenant's unsettled refunds, counted by every staff page.
        Index("ix_refunds_tenant_status_created", "tenant_id", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)  # the order system's number; not a local row
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="processing")
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # the payment system's refund id
    error: Mapped[str] = mapped_column(String(255), default="", server_default="")  # last refusal code or failure
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # times sent to the payment system
    # A refund nobody could settle for an hour, or one that failed after it was accepted: a person follows up.
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)  # where it was asked
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


LIVE_HANDOFF = "kind = 'handoff' AND status IN ('open', 'in_progress')"


class Ticket(Base):
    __tablename__ = "tickets"
    # At most one live handoff per conversation, enforced by the database: two concurrent
    # "转人工" requests both pass a check-then-insert, but only one insert can succeed.
    __table_args__ = (
        Index("uq_tickets_live_handoff", "conversation_id", unique=True,
              sqlite_where=text(LIVE_HANDOFF), postgresql_where=text(LIVE_HANDOFF)),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16), default="issue")  # issue | handoff
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | in_progress | closed
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The response time promised to the customer when the issue ticket was opened (not recomputed
    # from today's tenant settings), and what was done about it.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str] = mapped_column(Text, default="", server_default="")
    # A review request (kind "review", 申请复核): the assistant message whose decision is contested.
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[str] = mapped_column(String(16))  # up | down | correction
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    correction: Mapped[str] = mapped_column(Text, default="")
    reason_category: Mapped[str] = mapped_column(String(32), default="")
    attribution: Mapped[str] = mapped_column(String(32), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    # One rating per user and message (a second click changes it); the same correction sent
    # twice (double click, retry) is one correction. NULL on rows from before the key existed.
    dedup_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KBPatch(Base):
    __tablename__ = "kb_patches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    # correction: a knowledge entry drafted from a staff correction (feedback_id set);
    # questions: colloquial questions proposed as index entries of a document's chunks (W5, payload).
    kind: Mapped[str] = mapped_column(String(16), default="correction", server_default="correction")
    feedback_id: Mapped[int | None] = mapped_column(ForeignKey("feedback.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)  # what the reviewer reads
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | applied | rejected
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IndexQuestion(Base):
    """A colloquial question published as an extra index entry of a chunk (W5, after FastGPT's
    indexes): BM25, the vector search and the coverage gate also match the question against it.
    Bound to the document, the section heading and the exact chunk text it was approved for; when
    that text changes, the question stops counting until it is reviewed again."""

    __tablename__ = "index_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    heading: Mapped[str] = mapped_column(String(512), default="")
    text_sha: Mapped[str] = mapped_column(String(64))  # sha256 of the chunk text it was written for
    question: Mapped[str] = mapped_column(Text)
    embedding: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    patch_id: Mapped[int | None] = mapped_column(ForeignKey("kb_patches.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalCase(Base):
    __tablename__ = "eval_cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    suite: Mapped[str] = mapped_column(String(64), default="default")
    question: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="customer")
    as_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expected_tools: Mapped[list] = mapped_column(JSON, default=list)
    forbidden_tools: Mapped[list] = mapped_column(JSON, default=list)
    must_include: Mapped[list] = mapped_column(JSON, default=list)
    must_not_include: Mapped[list] = mapped_column(JSON, default=list)
    expect_refusal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expect_approval: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expected_doc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)  # earlier user turns, for multi-turn cases
    source: Mapped[str] = mapped_column(String(16), default="seed")  # seed | flywheel | manual
    feedback_id: Mapped[int | None] = mapped_column(ForeignKey("feedback.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | candidate | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeConflict(Base):
    """Two documents stating the same rule with different numbers (see rag/conflicts.py)."""

    __tablename__ = "knowledge_conflicts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[int] = mapped_column(Integer, index=True)
    chunk_id: Mapped[int] = mapped_column(Integer)
    other_document_id: Mapped[int] = mapped_column(Integer, index=True)
    other_chunk_id: Mapped[int] = mapped_column(Integer)
    sentence: Mapped[str] = mapped_column(Text)
    other_sentence: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | resolved
    # detected: found on ingestion; flywheel: a reviewer published a correction over it
    origin: Mapped[str] = mapped_column(String(16), default="detected", server_default="detected")
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnswerCache(Base):
    """Replayable answers keyed by question + knowledge version + config (see agent/answer_cache.py)."""

    __tablename__ = "answer_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16))  # public | staff
    question: Mapped[str] = mapped_column(String(512), index=True)
    kb_signature: Mapped[str] = mapped_column(String(128))
    config: Mapped[str] = mapped_column(String(32))
    answer: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # evidence + verification
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    suite: Mapped[str] = mapped_column(String(64), default="default")
    status: Mapped[str] = mapped_column(String(16), default="running")
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    model: Mapped[str] = mapped_column(String(128), default="")
    triggered_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvalResult(Base):
    __tablename__ = "eval_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eval_run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("eval_cases.id"))
    case_key: Mapped[str] = mapped_column(String(64))
    passed: Mapped[bool] = mapped_column(Boolean)
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    answer: Mapped[str] = mapped_column(Text, default="")
    tools_called: Mapped[list] = mapped_column(JSON, default=list)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class RateCounter(Base):
    """Attempts per key in a one-minute window, in the database so that every worker process
    shares the count (an in-process limiter lets N workers allow N times the limit)."""

    __tablename__ = "rate_counters"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of "login:<email>" etc.
    minute: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class ChannelEvent(Base):
    """Inbound channel events already accepted. Feishu re-sends an event it did not see
    acknowledged, with the same event_id, and the copy can reach another worker process or
    arrive after a restart; keeping the IDs here makes every process see it as a repeat."""

    __tablename__ = "channel_events"
    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # Also the inbox: a worker claims the event to answer it, and ops/channel_retry.py re-claims
    # one whose worker died after Feishu got its acknowledgement (Feishu will not resend it).
    # received | queued (waiting for an admission slot, N-189) | processing | done | failed;
    # rows from before the inbox were answered already.
    status: Mapped[str] = mapped_column(String(16), default="received", server_default="done")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Who changed what in a tenant's configuration and knowledge: risk limits, documents,
    published corrections, the regression gate, personal-data erasure. What the agent does
    is audited by runs, spans and approvals; this covers the administrators."""

    __tablename__ = "audit_logs"
    __table_args__ = (UniqueConstraint("tenant_id", "seq", name="uq_audit_logs_tenant_seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(100), default="")  # e.g. "document:12"
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # Tamper evidence (see app/audit.py): a per-tenant number without gaps, and an HMAC over the
    # entry with a key that is not stored in the database. NULL on entries from before.
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mac: Mapped[str | None] = mapped_column(String(64), nullable=True)



class McpServer(Base):
    """An external MCP server a tenant attached (Streamable HTTP). Its tools come in as TenantTool
    rows of kind "mcp", disabled until an administrator has set their risk and approval policy."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(48))
    url: Mapped[str] = mapped_column(String(512))
    auth: Mapped[dict] = mapped_column(JSON, default=dict)  # {"secret": "<ref>", "scheme": "bearer"}: a reference, never the secret
    region: Mapped[str] = mapped_column(String(16), default="")  # where it processes data (residency)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(500), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantTool(Base):
    """A tool a tenant declared (app/tooling): an HTTP endpoint of its own systems, or a tool of an
    MCP server it attached. Same risk tiers and approval flow as the built-in tools."""

    __tablename__ = "tenant_tools"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_tools_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(48))
    label: Mapped[str] = mapped_column(String(64), default="")  # what customers and reviewers read: "年费退还"
    description: Mapped[str] = mapped_column(Text, default="")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)  # JSON Schema, the subset in tooling/schema.py
    kind: Mapped[str] = mapped_column(String(8), default="http")  # http | mcp
    binding: Mapped[dict] = mapped_column(JSON, default=dict)  # http: method, url, auth, timeout, result, errors
    mcp_server_id: Mapped[int | None] = mapped_column(ForeignKey("mcp_servers.id"), nullable=True)
    remote_name: Mapped[str | None] = mapped_column(String(128), nullable=True)  # the tool's name on that server
    # The server's definition (description + schema) as an administrator reviewed it; a server that
    # later changes it gets the tool disabled until someone looks again.
    definition_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    risk: Mapped[str] = mapped_column(String(8), default="read")  # read | write | money
    approval: Mapped[dict] = mapped_column(JSON, default=dict)  # mode never|threshold|always, approver, limits
    roles: Mapped[list] = mapped_column(JSON, default=list)
    flags: Mapped[dict] = mapped_column(JSON, default=dict)  # sends_pii, returns_pii, region
    hints: Mapped[dict] = mapped_column(JSON, default=dict)  # intents (offline model), requires (prior tools)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | needs_review (MCP definition changed)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ToolExecution(Base):
    """LoopDesk's record of every write or money call a tenant tool sent, written before it is sent,
    under the idempotency key every attempt reuses (the refund ledger's pattern for any tool). An
    approval executes its call once: a retried resume finds this row and its key."""

    __tablename__ = "tool_executions"
    __table_args__ = (
        Index("uq_tool_executions_key", "idempotency_key", unique=True),
        Index("ix_tool_executions_approval", "approval_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    tool_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_tools.id"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(48))
    tool_version: Mapped[int] = mapped_column(Integer, default=1)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending (sent, no answer yet: it may have been done) | succeeded | failed (refused) | unknown
    status: Mapped[str] = mapped_column(String(16), default="pending")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)  # the output the model saw
    error: Mapped[str] = mapped_column(String(255), default="", server_default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Agent(Base):
    """The agent registry (HKMA ART, 2026: a registry of material AI agents, accountability by
    purpose, human intervention): who owns each agent, what it is for, which tools it may use,
    and a kill switch. A suspended agent answers nobody: every message goes to a person."""

    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_agents_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    key: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # the accountable person
    channels: Mapped[list] = mapped_column(JSON, default=list)  # web | feishu | voice | api
    tools: Mapped[list | None] = mapped_column(JSON, nullable=True)  # allowed tool names; NULL: every enabled tool
    risk_tier: Mapped[str] = mapped_column(String(16), default="high")  # low | medium | high (money actions)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | suspended
    suspended_reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    suspended_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
