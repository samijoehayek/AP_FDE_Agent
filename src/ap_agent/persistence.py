"""Where the loop keeps an invoice between steps, and what the duplicate check reads.

Responsible for two contracts the loop depends on, and an in-memory version of
each for tests. The Postgres versions are in ``ap_agent.db.store``.

**The store is the truth; the record in memory is a cache.** The loop reloads
the invoice from the store at the top of every iteration and saves it after
every transition. A state that only changes when a transition is saved is what
makes resume simple: a run that dies half way through a step leaves the invoice
in the state the step started from, so running it again restarts *that step* -
never the whole run, and never a step that already finished.

**Resume is keyed by the document's sha256.** One file is one invoice. Running
the same file again finds it: a terminal invoice is left alone, a halted one
carries on from where it stopped.

What this does not do: decide anything. It stores and returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from ap_agent.states.machine import InvoiceState, reachable_from

if TYPE_CHECKING:
    from ap_agent.contracts.dedup import DedupEntry
    from ap_agent.contracts.run import InvoiceRecord

__all__ = [
    "NUMBER_CLAIMING_STATES",
    "DedupIndex",
    "InMemoryDedupIndex",
    "InMemoryInvoiceStore",
    "InvoiceStore",
    "UnknownInvoiceError",
]

NUMBER_CLAIMING_STATES: frozenset[InvoiceState] = frozenset(
    ({InvoiceState.DUPLICATE_CHECKED} | reachable_from(InvoiceState.DUPLICATE_CHECKED))
    - {InvoiceState.REJECTED, InvoiceState.CANCELLED}
)
"""States in which an invoice holds its ``(vendor_id, invoice_number)``.

``invoices`` is unique on that pair - the duplicate control the database
enforces. An invoice claims its number only once it has passed the duplicate
check, so a second copy is held at ON_HOLD_DUPLICATE for a person instead of
failing at the database, and a rejected or cancelled one gives the number back.
"""


class UnknownInvoiceError(LookupError):
    """The store has no invoice with this id."""


class InvoiceStore(Protocol):
    """Durable invoice state. The loop's source of truth."""

    def find_by_document(self, sha256: str) -> InvoiceRecord | None:
        """The invoice this document became, or None if it has never arrived."""
        ...

    def create(
        self, record: InvoiceRecord, *, document_sha256: str, byte_size: int, storage_uri: str
    ) -> None:
        """Record a newly arrived document and the invoice it starts."""
        ...

    def load(self, invoice_id: str) -> InvoiceRecord:
        """The invoice as stored, its state authoritative."""
        ...

    def save(self, record: InvoiceRecord) -> None:
        """Persist the record after a transition."""
        ...

    def start_run(self, run_id: str, invoice_id: str) -> None:
        """Record that a run began."""
        ...

    def finish_run(
        self, run_id: str, *, step_count: int, last_step: str | None, last_state: InvoiceState
    ) -> None:
        """Record how a run ended."""
        ...


class DedupIndex(Protocol):
    """What the duplicate check compares against."""

    def candidates(self, entry: DedupEntry, window_days: int) -> list[DedupEntry]:
        """Same-vendor entries within ``window_days``, or with the same number at any date.

        May include ``entry`` itself; the rule excludes it by id.
        """
        ...

    def add(self, entry: DedupEntry) -> None:
        """Record an entry. Idempotent per invoice."""
        ...


@dataclass
class InMemoryInvoiceStore:
    """An invoice store that lives as long as the process. For tests."""

    records: dict[str, InvoiceRecord] = field(default_factory=dict[str, "InvoiceRecord"])
    documents: dict[str, str] = field(default_factory=dict[str, str])
    runs: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])

    def find_by_document(self, sha256: str) -> InvoiceRecord | None:
        """The invoice for this document, or None."""
        invoice_id = self.documents.get(sha256)
        return self.records[invoice_id] if invoice_id else None

    def create(
        self, record: InvoiceRecord, *, document_sha256: str, byte_size: int, storage_uri: str
    ) -> None:
        """Index the document and store the record."""
        del byte_size, storage_uri
        self.documents[document_sha256] = str(record.invoice_id)
        self.records[str(record.invoice_id)] = record

    def load(self, invoice_id: str) -> InvoiceRecord:
        """The stored record.

        Raises:
            UnknownInvoiceError: Nothing stored under this id.
        """
        try:
            return self.records[invoice_id]
        except KeyError as exc:
            raise UnknownInvoiceError(invoice_id) from exc

    def save(self, record: InvoiceRecord) -> None:
        """Replace the stored record."""
        self.records[str(record.invoice_id)] = record

    def start_run(self, run_id: str, invoice_id: str) -> None:
        """Note the run."""
        self.runs[run_id] = {"invoice_id": invoice_id, "ended": False}

    def finish_run(
        self, run_id: str, *, step_count: int, last_step: str | None, last_state: InvoiceState
    ) -> None:
        """Note how it ended."""
        self.runs[run_id].update(
            ended=True, step_count=step_count, last_step=last_step, last_state=last_state
        )


@dataclass
class InMemoryDedupIndex:
    """A duplicate index that lives as long as the process. For tests."""

    entries: dict[str, DedupEntry] = field(default_factory=dict[str, "DedupEntry"])

    def candidates(self, entry: DedupEntry, window_days: int) -> list[DedupEntry]:
        """Same vendor, inside the window or with the same number."""
        window = timedelta(days=window_days)
        return [
            other
            for other in self.entries.values()
            if other.vendor_id == entry.vendor_id
            and (
                abs(other.invoice_date - entry.invoice_date) <= window
                or other.normalised_invoice_number == entry.normalised_invoice_number
            )
        ]

    def add(self, entry: DedupEntry) -> None:
        """Store the entry; a second add for the same invoice changes nothing."""
        self.entries.setdefault(entry.invoice_id, entry)
