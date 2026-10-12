"""The Postgres implementations of the invoice store and the duplicate index.

Responsible for reading and writing ``documents``, ``invoices``, ``runs`` and
``dedup_index`` as the application role. The contracts and the reasoning are in
:mod:`ap_agent.persistence`.

**The full record lives in ``payloads``**, referenced by ``invoices.record_ref``:
everything the loop knows, content-addressed and stored before the row that
cites it. The other ``invoices`` columns are what gets queried, and ``state`` is
authoritative - a record whose stored JSON disagrees with the column takes the
column's state.

What it does not do: delete. The role cannot.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from ulid import ULID

from ap_agent.contracts.dedup import DedupEntry
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.db.models import Approval, ApprovalDecision, Document, Extraction, Invoice, Run
from ap_agent.db.models import DedupEntry as DedupRow
from ap_agent.payloads import parse_ref, store_ref
from ap_agent.persistence import (
    NUMBER_CLAIMING_STATES,
    ApprovalDecisionRefusedError,
    ApprovalView,
    UnknownInvoiceError,
    outcome,
)
from ap_agent.states.machine import InvoiceState

if TYPE_CHECKING:
    from collections.abc import Generator
    from decimal import Decimal
    from typing import Literal

    from sqlalchemy.orm import Session, sessionmaker

    from ap_agent.contracts.approval import ApprovalRequirement
    from ap_agent.payloads import PayloadStore

__all__ = ["PostgresApprovalStore", "PostgresDedupIndex", "PostgresInvoiceStore"]


@dataclass(frozen=True)
class PostgresInvoiceStore:
    """Invoice state in Postgres; the full record as a payload."""

    sessions: sessionmaker[Session]
    payloads: PayloadStore

    def find_by_document(self, sha256: str) -> InvoiceRecord | None:
        """The invoice this document became, or None."""
        with self.sessions() as session:
            invoice_id = session.execute(
                select(Invoice.invoice_id).where(Invoice.document_sha256 == sha256)
            ).scalar_one_or_none()
        return self.load(invoice_id) if invoice_id else None

    def create(
        self, record: InvoiceRecord, *, document_sha256: str, byte_size: int, storage_uri: str
    ) -> None:
        """Write the document and the invoice in one transaction, record first as a payload."""
        record_ref = parse_ref(store_ref(self.payloads, record))
        with self.sessions() as session, session.begin():
            session.execute(
                insert(Document)
                .values(sha256=document_sha256, storage_uri=storage_uri, byte_size=byte_size)
                .on_conflict_do_nothing(index_elements=[Document.sha256])
            )
            session.execute(
                insert(Invoice).values(
                    invoice_id=str(record.invoice_id),
                    document_sha256=document_sha256,
                    state=record.state.value,
                    record_ref=record_ref,
                )
            )

    def load(self, invoice_id: str) -> InvoiceRecord:
        """The stored record, with the column's state.

        Raises:
            UnknownInvoiceError: No such invoice, or its record payload is missing.
        """
        with self.sessions() as session:
            row = session.execute(
                select(Invoice.state, Invoice.record_ref).where(Invoice.invoice_id == invoice_id)
            ).one_or_none()
        if row is None or row.record_ref is None:
            raise UnknownInvoiceError(invoice_id)
        stored = self.payloads.get(row.record_ref)
        if stored is None:
            msg = f"{invoice_id}: record payload {row.record_ref} missing"
            raise UnknownInvoiceError(msg)
        record = InvoiceRecord.model_validate_json(stored.content)
        return record.model_copy(update={"state": InvoiceState(row.state)})

    def save(self, record: InvoiceRecord) -> None:
        """Store the record, then point the invoice at it and update what is queried.

        The ``(vendor_id, invoice_number)`` pair is claimed only in
        :data:`~ap_agent.persistence.NUMBER_CLAIMING_STATES`. Before the
        duplicate check, or once rejected, the number column is empty.
        """
        record_ref = parse_ref(store_ref(self.payloads, record))
        extraction = record.extraction
        claims = record.state in NUMBER_CLAIMING_STATES
        total = extraction.total if extraction is not None else None
        values: dict[str, object] = {
            "state": record.state.value,
            "record_ref": record_ref,
            "vendor_id": record.vendor_id,
            "vendor_name": record.vendor_match.vendor_name if record.vendor_match else None,
            "invoice_number": extraction.invoice_number if extraction and claims else None,
            "invoice_date": (
                record.invoice_date_resolved
                or (extraction.invoice_date if extraction is not None else None)
            ),
            "currency": extraction.currency if extraction is not None else None,
            # The column refuses negatives; a credit note's total stays on the record.
            "total": total if total is not None and total >= 0 else None,
            "po_number": record.purchase_order.po_number if record.purchase_order else None,
            "updated_at": func.now(),
        }
        with self.sessions() as session, session.begin():
            session.execute(
                update(Invoice).where(Invoice.invoice_id == str(record.invoice_id)).values(**values)
            )
            if record.ingest is not None:
                session.execute(
                    update(Document)
                    .where(Document.sha256 == record.ingest.sha256)
                    .values(
                        media_type=record.ingest.media_type,
                        page_count=record.ingest.page_count,
                        has_text_layer=record.ingest.has_text_layer,
                        pages_with_text=record.ingest.pages_with_text,
                        min_sharpness=record.ingest.min_sharpness,
                    )
                )

    def record_extraction(
        self,
        invoice_id: str,
        *,
        vision_ref: str,
        text_ref: str | None,
        confidence_ref: str | None,
        auto_ok: bool | None,
    ) -> None:
        """Insert an extractions row. The refs are payloads already stored."""
        with self.sessions() as session, session.begin():
            session.execute(
                insert(Extraction).values(
                    extraction_id=str(ULID()),
                    invoice_id=invoice_id,
                    vision_ref=parse_ref(vision_ref),
                    text_ref=parse_ref(text_ref) if text_ref else None,
                    confidence_ref=parse_ref(confidence_ref) if confidence_ref else None,
                    auto_ok=auto_ok,
                )
            )

    def prior_invoice_count(self, vendor_id: str, *, excluding: str) -> int:
        """Other invoices with this vendor, whatever their state."""
        with self.sessions() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(Invoice)
                    .where(Invoice.vendor_id == vendor_id, Invoice.invoice_id != excluding)
                ).scalar_one()
            )

    def start_run(self, run_id: str, invoice_id: str) -> None:
        """Insert the run row, once. A recovered run's start step may run twice."""
        with self.sessions() as session, session.begin():
            session.execute(
                insert(Run)
                .values(run_id=run_id, invoice_id=invoice_id, step_count=0)
                .on_conflict_do_nothing(index_elements=[Run.run_id])
            )

    def unfinished_runs(self) -> list[tuple[str, str]]:
        """Every run with no ``ended_at``, oldest first."""
        with self.sessions() as session:
            rows = session.execute(
                select(Run.run_id, Run.invoice_id)
                .where(Run.ended_at.is_(None))
                .order_by(Run.started_at)
            ).all()
        return [(row.run_id, row.invoice_id) for row in rows]

    def abandon_run(self, run_id: str) -> None:
        """End an unfinished run as ``abandoned``, leaving its counts as they were."""
        with self.sessions() as session, session.begin():
            session.execute(
                update(Run)
                .where(Run.run_id == run_id, Run.ended_at.is_(None))
                .values(ended_at=func.now(), last_step="abandoned")
            )

    @contextmanager
    def lock(self, invoice_id: str) -> Generator[bool]:
        """A session-level advisory lock on the invoice, for as long as the block runs.

        Held on its own connection in autocommit, so it is not an open
        transaction, and Postgres releases it when the connection goes - a
        process that dies cannot leave an invoice locked. That is what makes
        "the lock is free" mean "nobody is running this invoice", which is the
        fact recovering a run depends on.
        """
        key = func.hashtextextended(invoice_id, 0)
        with self.sessions() as session:
            connection = session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
            held = bool(connection.execute(select(func.pg_try_advisory_lock(key))).scalar())
            try:
                yield held
            finally:
                if held:
                    connection.execute(select(func.pg_advisory_unlock(key)))

    def unfinished_run(self, invoice_id: str) -> str | None:
        """The run on this invoice with no ``ended_at``. At most one: revision 0006."""
        with self.sessions() as session:
            return session.execute(
                select(Run.run_id).where(Run.invoice_id == invoice_id, Run.ended_at.is_(None))
            ).scalar_one_or_none()

    def finish_run(
        self, run_id: str, *, step_count: int, last_step: str | None, last_state: InvoiceState
    ) -> None:
        """Close the run row."""
        with self.sessions() as session, session.begin():
            session.execute(
                update(Run)
                .where(Run.run_id == run_id)
                .values(
                    ended_at=func.now(),
                    step_count=step_count,
                    last_step=last_step,
                    last_state=last_state.value,
                )
            )


@dataclass(frozen=True)
class PostgresDedupIndex:
    """``dedup_index`` in Postgres. Insert and read only."""

    sessions: sessionmaker[Session]

    def candidates(self, entry: DedupEntry, window_days: int) -> list[DedupEntry]:
        """Same vendor, inside the window or with the same number at any date."""
        window = timedelta(days=window_days)
        statement = select(DedupRow).where(
            DedupRow.vendor_id == entry.vendor_id,
            or_(
                DedupRow.invoice_date.between(
                    entry.invoice_date - window, entry.invoice_date + window
                ),
                DedupRow.normalised_invoice_number == entry.normalised_invoice_number,
            ),
        )
        with self.sessions() as session:
            rows = session.execute(statement).scalars().all()
        return [
            DedupEntry(
                invoice_id=row.invoice_id,
                vendor_id=row.vendor_id,
                normalised_invoice_number=row.normalised_invoice_number,
                amount=row.amount,
                currency=row.currency,
                invoice_date=row.invoice_date,
            )
            for row in rows
        ]

    def add(self, entry: DedupEntry) -> None:
        """Insert; a second add for the same invoice changes nothing."""
        with self.sessions() as session, session.begin():
            session.execute(
                insert(DedupRow)
                .values(**entry.model_dump())
                .on_conflict_do_nothing(index_elements=[DedupRow.invoice_id])
            )


@dataclass(frozen=True)
class PostgresApprovalStore:
    """``approvals`` and ``approval_decisions`` in Postgres.

    A decision and the status it produces are written in one transaction, with
    the request row locked: two approvers deciding at once cannot both be the
    one that "completed" it, and a status can never disagree with the decisions
    it summarises.
    """

    sessions: sessionmaker[Session]

    def request(  # noqa: PLR0913 - one keyword per column
        self,
        approval_id: str,
        invoice_id: str,
        requirement: ApprovalRequirement,
        *,
        amount: Decimal,
        currency: str,
        config_version: str,
    ) -> tuple[ApprovalView, bool]:
        """Insert the request, or find the one already under this id."""
        with self.sessions() as session, session.begin():
            created = (
                session.execute(
                    insert(Approval)
                    .values(
                        approval_id=approval_id,
                        invoice_id=invoice_id,
                        required_approvers=requirement.required_approvers,
                        approver_role=requirement.approver_role,
                        amount=amount,
                        currency=currency,
                        config_version=config_version,
                        rule_basis=requirement.basis,
                        status="pending",
                    )
                    .on_conflict_do_nothing(index_elements=[Approval.approval_id])
                    .returning(Approval.approval_id)
                ).scalar_one_or_none()
                is not None
            )
        view = self.get(approval_id)
        assert view is not None  # noqa: S101 - inserted or already present, one line up
        return view, created

    def get(self, approval_id: str) -> ApprovalView | None:
        """The request and its decisions, or None."""
        with self.sessions() as session:
            return _view(session, approval_id)

    def decide(
        self, approval_id: str, *, user_id: str, decision: Literal["approve", "reject"], reason: str
    ) -> ApprovalView:
        """Record the decision and, if it completes the request, the outcome."""
        with self.sessions() as session, session.begin():
            row = session.execute(
                select(Approval).where(Approval.approval_id == approval_id).with_for_update()
            ).scalar_one()
            if row.status != "pending":
                msg = f"approval {approval_id} is {row.status}, not pending"
                raise ApprovalDecisionRefusedError(msg)
            already = session.execute(
                select(ApprovalDecision.decided_by).where(
                    ApprovalDecision.approval_id == approval_id,
                    ApprovalDecision.decided_by == user_id,
                )
            ).first()
            if already is not None:
                msg = f"{user_id} has already decided approval {approval_id}"
                raise ApprovalDecisionRefusedError(msg)
            session.execute(
                insert(ApprovalDecision).values(
                    decision_id=str(ULID()),
                    approval_id=approval_id,
                    decided_by=user_id,
                    decision=decision,
                    reason=reason,
                )
            )
            view = _view(session, approval_id)
            assert view is not None  # noqa: S101 - locked above
            if view.status != "pending":
                session.execute(
                    update(Approval)
                    .where(Approval.approval_id == approval_id)
                    .values(
                        status=view.status,
                        decided_at=func.now(),
                        decided_by=user_id,
                        decision=decision,
                        reason=reason,
                    )
                )
        final = self.get(approval_id)
        assert final is not None  # noqa: S101
        return final

    def cancel(self, approval_id: str, *, by: str, reason: str) -> None:
        """Withdraw a pending request. ``by`` names the rule that superseded it."""
        with self.sessions() as session, session.begin():
            session.execute(
                update(Approval)
                .where(Approval.approval_id == approval_id, Approval.status == "pending")
                .values(status="cancelled", decided_at=func.now(), decided_by=by, reason=reason)
            )


def _view(session: Session, approval_id: str) -> ApprovalView | None:
    """Build the view from the request row and its decision rows."""
    row = session.execute(
        select(Approval).where(Approval.approval_id == approval_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    decisions = session.execute(
        select(ApprovalDecision.decided_by, ApprovalDecision.decision)
        .where(ApprovalDecision.approval_id == approval_id)
        .order_by(ApprovalDecision.decided_at, ApprovalDecision.decision_id)
    ).all()
    approvals = tuple(user for user, decision in decisions if decision == "approve")
    rejected_by = next((user for user, decision in decisions if decision == "reject"), None)
    status = (
        row.status
        if row.status == "cancelled"
        else outcome(row.required_approvers, len(approvals), rejected_by is not None)
    )
    return ApprovalView(
        approval_id=row.approval_id,
        invoice_id=row.invoice_id,
        requested_at=row.requested_at,
        required_approvers=row.required_approvers,
        approver_role=row.approver_role,
        status=status,
        approvals=approvals,
        rejected_by=rejected_by,
    )
