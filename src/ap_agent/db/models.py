"""Tables.

Four decisions worth reading before adding a fifth table:

* ``audit_events`` is append-only at the schema level, not by convention. It
  carries a unique ``(invoice_id, run_id, step_seq)`` so a duplicate step is a
  database error, and revision ``0002`` revokes ``UPDATE``, ``DELETE`` and
  ``TRUNCATE`` on it from the application role. Note *which* role: the
  application connects as an ordinary role rather than the schema owner,
  because a superuser bypasses privilege checks and the revoke would be
  decorative. Enforcement in the schema survives a refactor; a comment does
  not.
* Money is ``Numeric(18, 2)`` and currency travels with it. There is no
  application-wide currency constant.
* ``erp_writes`` exists so rule 5 of ``CLAUDE.md`` has somewhere to live. Every
  ERP write claims a unique idempotency key before it calls out, so a retry after
  a timeout finds the prior row instead of posting twice.
* Approvals are rows, not callbacks. A four-day approval must survive a deploy.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ap_agent.db.base import Base
from ap_agent.states.machine import InvoiceState

_ULID_LEN = 26
_ID_LEN = 64


class Document(Base):
    """A file that arrived, identified by the hash of its bytes."""

    __tablename__ = "documents"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    has_text_layer: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pages_with_text: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_sharpness: Mapped[float | None] = mapped_column(nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("page_count >= 1", name="page_count_positive"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
    )


class Invoice(Base):
    """One invoice, and the state it is currently in.

    ``state`` is stored as text and validated by the transition table in code,
    not by a database enum: adding a state should not require a migration and a
    deploy in lockstep, and the table in ``ap_agent.states.machine`` is the spec
    either way.
    """

    __tablename__ = "invoices"

    invoice_id: Mapped[str] = mapped_column(String(_ULID_LEN), primary_key=True)
    document_sha256: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.sha256"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=InvoiceState.RECEIVED.value
    )

    vendor_id: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    # Date, not DateTime. An invoice date is a calendar date printed on paper:
    # it has no time and no timezone, and storing it as timestamptz invents both.
    # That matters here - invoice_date drives payment terms and the duplicate
    # detection window, so a timezone conversion that shifts it by a day is a
    # wrong due date or a missed duplicate.
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    extraction: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    match_result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    config_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # The primary duplicate control, enforced by the database rather than by
        # whichever code path happens to run first.
        UniqueConstraint("vendor_id", "invoice_number"),
        Index("ix_invoices_state", "state"),
        Index("ix_invoices_vendor_id", "vendor_id"),
        CheckConstraint("total IS NULL OR total >= 0", name="total_non_negative"),
    )


class AuditEventRow(Base):
    """One entry in an invoice's hash-chained history. Append only.

    Mirrors :class:`~ap_agent.contracts.audit.AuditEvent`. Revision ``0002``
    grants the application role SELECT and INSERT here and nothing else - the ORM
    cannot be the thing that keeps this append-only, because the ORM is what a
    future bug will be written in.
    """

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(_ULID_LEN), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(_ULID_LEN), nullable=False)
    run_id: Mapped[str] = mapped_column(String(_ID_LEN), nullable=False)
    step_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)

    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)

    input_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    output_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)

    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_args_redacted: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    tool_result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)

    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    prev_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("invoice_id", "run_id", "step_seq"),
        Index("ix_audit_events_invoice_id_step_seq", "invoice_id", "step_seq"),
        Index("ix_audit_events_ts_utc", "ts_utc"),
        CheckConstraint("step_seq >= 0", name="step_seq_non_negative"),
    )


class ApprovalRequest(Base):
    """A durable human-in-the-loop approval.

    Durable because approvals take days. Nothing about this row assumes a
    process is still running, and ``decided_by`` is never a service account -
    only a human approves.
    """

    __tablename__ = "approval_requests"

    approval_id: Mapped[str] = mapped_column(String(_ULID_LEN), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(
        String(_ULID_LEN), ForeignKey("invoices.invoice_id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    approver_user_id: Mapped[str] = mapped_column(String(_ID_LEN), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by_user_id: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    due_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        Index("ix_approval_requests_approver_status", "approver_user_id", "status"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')",
            name="status_known",
        ),
        CheckConstraint(
            "status = 'pending' OR decided_by_user_id IS NOT NULL",
            name="decided_requires_human",
        ),
    )


class ErpWrite(Base):
    """The idempotency ledger for outbound writes.

    A row is claimed before the call goes out and completed after it returns. A
    retry that finds a claimed row waits or reuses the result rather than
    issuing a second write - which is the whole of rule 5.
    """

    __tablename__ = "erp_writes"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String(_ULID_LEN), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="claimed")

    external_id: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_erp_writes_invoice_id", "invoice_id"),
        CheckConstraint(
            "status IN ('claimed', 'succeeded', 'failed')",
            name="status_known",
        ),
    )
