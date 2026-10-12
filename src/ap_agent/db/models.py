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
* Rule 5 of ``CLAUDE.md``: every ERP write claims a unique idempotency key before
  it calls out. For a bill the key is ``invoices.erp_idempotency_key`` (revision
  ``0007``), because it names the bill - one per vendor and number - rather than
  the call. ``erp_writes``, the per-call ledger, is not written yet.
* Approvals are rows, not callbacks. A four-day approval must survive a deploy.
* ``payloads`` is content-addressed: the primary key is the sha256 of the bytes,
  so every ``output_ref`` on the trail can be resolved to what was hashed, and
  the application role may insert and read it but never change it.
* Nothing in this schema is deleted by the application. Revision ``0004``
  withdraws DELETE from the application role on every table: rule 3 of
  ``CLAUDE.md`` says no tool deletes anything, and a grant is where that holds.
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
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ap_agent.db.base import Base
from ap_agent.states.machine import InvoiceState

_ULID_LEN = 26
_ID_LEN = 64


class Document(Base):
    """A file that arrived, identified by the hash of its bytes.

    The row is written when the file arrives - that is what lets a second run of
    the same file find its invoice - so what only parsing can tell (type, pages,
    text layer) is null until intake fills it in. Zero pages means intake
    refused the file before opening it.
    """

    __tablename__ = "documents"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_text_layer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pages_with_text: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_sharpness: Mapped[float | None] = mapped_column(nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="page_count_known"),
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
    po_number: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    erp_bill_id: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    """The ERP's id for the bill this invoice became. Set once, by create_bill."""
    erp_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """``bill:<vendor_id>:<normalised number>``, written before QuickBooks is called.

    Unique: one bill per vendor and number, whichever invoice id asks for it.
    """
    record_ref: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("payloads.sha256"), nullable=True
    )
    """The latest full ``InvoiceRecord``, as a payload. The columns above are what
    is queried; this is what a resumed run is rebuilt from."""
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
        # One invoice per document. Running the same file again resumes it.
        UniqueConstraint("document_sha256"),
        # One bill per vendor and invoice number. Revision 0007.
        UniqueConstraint("erp_idempotency_key"),
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
        # One row per position in a run. A durable run's steps are tried at least
        # once; this is what makes the second try write nothing (revision 0006).
        UniqueConstraint("run_id", "step_seq"),
        Index("ix_audit_events_invoice_id_step_seq", "invoice_id", "step_seq"),
        Index("ix_audit_events_ts_utc", "ts_utc"),
        CheckConstraint("step_seq >= 0", name="step_seq_non_negative"),
    )


class Approval(Base):
    """One request for approval, and how it ended.

    Durable because approvals take days: nothing here assumes a process is still
    running. ``required_approvers`` and ``approver_role`` come from the approval
    matrix - the count and the tier - and ``rule_basis`` says which rule and
    which amount produced them.

    **Completion is derived from** :class:`ApprovalDecision` **rows, never set
    directly.** The first ``reject`` completes the request as rejected - one
    person saying no is enough. Otherwise it is approved once the number of
    distinct people who approved reaches ``required_approvers``. ``status``,
    ``decided_by``, ``decided_at``, ``decision`` and ``reason`` record the
    decision that completed it, written in the same transaction as that
    decision's row; ``decided_by`` is never a service account.
    """

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(_ULID_LEN), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(
        String(_ULID_LEN), ForeignKey("invoices.invoice_id"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    required_approvers: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_role: Mapped[str] = mapped_column(String(_ID_LEN), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_basis: Mapped[str] = mapped_column(Text, nullable=False)
    """Why this many approvers of this role: the rule id and the amount compared."""

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_approvals_invoice_id", "invoice_id"),
        Index("ix_approvals_status", "status"),
        CheckConstraint("required_approvers BETWEEN 1 AND 4", name="required_approvers_range"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')", name="status_known"
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'reject')", name="decision_known"
        ),
        CheckConstraint(
            "status = 'pending' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="decided_requires_human",
        ),
    )


class ApprovalDecision(Base):
    """One person's decision on one approval request. Inserted, never changed.

    ``(approval_id, decided_by)`` is unique: a tier that needs two approvers
    needs two *people*, and one person approving twice is one approval.
    """

    __tablename__ = "approval_decisions"

    decision_id: Mapped[str] = mapped_column(String(_ULID_LEN), primary_key=True)
    approval_id: Mapped[str] = mapped_column(
        String(_ULID_LEN), ForeignKey("approvals.approval_id"), nullable=False
    )
    decided_by: Mapped[str] = mapped_column(String(_ID_LEN), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("approval_id", "decided_by"),
        CheckConstraint("decision IN ('approve', 'reject')", name="decision_known"),
        CheckConstraint("length(btrim(reason)) > 0", name="reason_required"),
    )


class Extraction(Base):
    """The readings of one invoice, by reference, and what a person corrected.

    The readings themselves are payloads; this row says which ones belong
    together. ``human_corrections`` is the transcription a person supplied at
    NEEDS_HUMAN_EXTRACTION - the golden set grows from these.
    """

    __tablename__ = "extractions"

    extraction_id: Mapped[str] = mapped_column(String(_ULID_LEN), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(
        String(_ULID_LEN), ForeignKey("invoices.invoice_id"), nullable=False
    )
    vision_ref: Mapped[str] = mapped_column(
        String(64), ForeignKey("payloads.sha256"), nullable=False
    )
    text_ref: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("payloads.sha256"), nullable=True
    )
    confidence_ref: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("payloads.sha256"), nullable=True
    )
    auto_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    human_corrections: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    corrected_by: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_extractions_invoice_id", "invoice_id"),
        CheckConstraint(
            "(human_corrections IS NULL) = (corrected_by IS NULL)",
            name="correction_has_an_author",
        ),
    )


class Run(Base):
    """One invocation of the loop on one invoice. ``run_id`` is the trail's run id."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(_ID_LEN), primary_key=True)
    invoice_id: Mapped[str] = mapped_column(
        String(_ULID_LEN), ForeignKey("invoices.invoice_id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_step: Mapped[str | None] = mapped_column(String(_ID_LEN), nullable=True)
    """The last event applied, e.g. ``match`` or ``halt_no_tool``."""
    last_state: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("ix_runs_invoice_id", "invoice_id"),
        # At most one unfinished run per invoice: `just run` on a document whose
        # run never finished attaches to that run instead of starting a second
        # one that would drive the same invoice (revision 0006).
        Index(
            "uq_runs_invoice_id_unfinished",
            "invoice_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        CheckConstraint("step_count >= 0", name="step_count_non_negative"),
    )


class DedupEntry(Base):
    """What the duplicate check compares: one row per invoice that got that far.

    Not unique on anything but the invoice. Near-duplicates are *detected* here
    and held for a person; exact ``(vendor_id, invoice_number)`` duplicates are
    *prevented* by the constraint on ``invoices``.
    """

    __tablename__ = "dedup_index"

    invoice_id: Mapped[str] = mapped_column(
        String(_ULID_LEN), ForeignKey("invoices.invoice_id"), primary_key=True
    )
    vendor_id: Mapped[str] = mapped_column(String(_ID_LEN), nullable=False)
    normalised_invoice_number: Mapped[str] = mapped_column(String(_ID_LEN), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_dedup_index_vendor_id_invoice_date", "vendor_id", "invoice_date"),
        Index("ix_dedup_index_invoice_date", "invoice_date"),
    )


class Payload(Base):
    """Bytes, addressed by their sha256. What every ``output_ref`` points at.

    Immutable by construction: the key *is* the content, so a second write of
    the same bytes is the same row, and the application role has no UPDATE or
    DELETE on this table to make it otherwise.
    """

    __tablename__ = "payloads"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column("bytes", LargeBinary, nullable=False)
    """The column is ``bytes``; the attribute is not, so it does not shadow the builtin."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_is_hex"),)


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
