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

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from ap_agent.contracts.dedup import DedupEntry
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.db.models import DedupEntry as DedupRow
from ap_agent.db.models import Document, Invoice, Run
from ap_agent.payloads import parse_ref, store_ref
from ap_agent.persistence import NUMBER_CLAIMING_STATES, UnknownInvoiceError
from ap_agent.states.machine import InvoiceState

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from ap_agent.payloads import PayloadStore

__all__ = ["PostgresDedupIndex", "PostgresInvoiceStore"]


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

    def start_run(self, run_id: str, invoice_id: str) -> None:
        """Insert the run row."""
        with self.sessions() as session, session.begin():
            session.execute(insert(Run).values(run_id=run_id, invoice_id=invoice_id, step_count=0))

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
