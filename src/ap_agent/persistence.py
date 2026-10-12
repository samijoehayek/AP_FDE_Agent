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

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

from ap_agent.states.machine import InvoiceState, reachable_from

if TYPE_CHECKING:
    from collections.abc import Generator
    from contextlib import AbstractContextManager
    from decimal import Decimal

    from ap_agent.contracts.approval import ApprovalRequirement
    from ap_agent.contracts.dedup import DedupEntry
    from ap_agent.contracts.run import InvoiceRecord

__all__ = [
    "CURRENT_APPROVAL_STORE",
    "NUMBER_CLAIMING_STATES",
    "ApprovalDecisionRefusedError",
    "ApprovalStore",
    "ApprovalView",
    "DedupIndex",
    "InMemoryApprovalStore",
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


class ApprovalDecisionRefusedError(ValueError):
    """A decision the approvals store will not record: decided already, or a second vote."""


ApprovalStatus = Literal["pending", "approved", "rejected", "cancelled"]


@dataclass(frozen=True)
class ApprovalView:
    """One approval request as stored, with the decisions on it so far."""

    approval_id: str
    invoice_id: str
    requested_at: datetime
    required_approvers: int
    approver_role: str
    status: ApprovalStatus
    approvals: tuple[str, ...]
    """Who has approved, in order. Distinct people, by construction."""
    rejected_by: str | None = None


def outcome(required: int, approvals: int, rejected: bool) -> ApprovalStatus:
    """Completion, derived from the decisions - never set directly.

    One reject completes the request as rejected: one person saying no is
    enough. Otherwise it is approved when distinct approvers reach the count.
    """
    if rejected:
        return "rejected"
    return "approved" if approvals >= required else "pending"


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
        """Record that a run began. A second call for the same run changes nothing."""
        ...

    def unfinished_run(self, invoice_id: str) -> str | None:
        """The run that began on this invoice and never ended, if there is one."""
        ...

    def unfinished_runs(self) -> list[tuple[str, str]]:
        """Every ``(run_id, invoice_id)`` that began and never ended."""
        ...

    def abandon_run(self, run_id: str) -> None:
        """Close a run nothing will ever finish, as ``abandoned``. Its counts stand."""
        ...

    def lock(self, invoice_id: str) -> AbstractContextManager[bool]:
        """Hold the invoice for one process while the block runs. Yields False if taken.

        Not a wait: a second process is told the invoice is busy and stops.
        """
        ...

    def record_extraction(
        self,
        invoice_id: str,
        *,
        vision_ref: str,
        text_ref: str | None,
        confidence_ref: str | None,
        auto_ok: bool | None,
    ) -> None:
        """Record which readings, by payload ref, belong to this invoice."""
        ...

    def prior_invoice_count(self, vendor_id: str, *, excluding: str) -> int:
        """How many other invoices this vendor has in the store."""
        ...

    def finish_run(
        self, run_id: str, *, step_count: int, last_step: str | None, last_state: InvoiceState
    ) -> None:
        """Record how a run ended."""
        ...


class ApprovalStore(Protocol):
    """Durable approval requests and the decisions on them."""

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
        """Open a request, or return the one already under this id. True if created."""
        ...

    def get(self, approval_id: str) -> ApprovalView | None:
        """The request, or None."""
        ...

    def decide(
        self, approval_id: str, *, user_id: str, decision: Literal["approve", "reject"], reason: str
    ) -> ApprovalView:
        """Record one person's decision and return the request as it now stands.

        Raises:
            ApprovalDecisionRefusedError: Not pending, or this person already decided.
        """
        ...

    def cancel(self, approval_id: str, *, by: str, reason: str) -> None:
        """Withdraw a pending request: superseded, not decided."""
        ...


CURRENT_APPROVAL_STORE: ContextVar[ApprovalStore] = ContextVar("current_approval_store")
"""The approval store for the run in progress.

``request_approval`` is a tool, and a tool takes exactly one argument - its
input model. The loop binds the run's store here for the length of the call,
so the tool writes where the run is writing, and a test's in-memory store is
never confused with Postgres. Unset outside a run, and the tool refuses then.
"""


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
    extractions: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

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
        """Note the run, once."""
        self.runs.setdefault(run_id, {"invoice_id": invoice_id, "ended": False})

    def unfinished_runs(self) -> list[tuple[str, str]]:
        """Every run not yet ended."""
        return [
            (run_id, str(run["invoice_id"]))
            for run_id, run in self.runs.items()
            if not run["ended"]
        ]

    def abandon_run(self, run_id: str) -> None:
        """Close it as abandoned."""
        self.runs[run_id].update(ended=True, last_step="abandoned")

    @contextmanager
    def lock(self, invoice_id: str) -> Generator[bool]:  # noqa: ARG002 - one process, no contention
        """Always held: an in-memory store has one process."""
        yield True

    def unfinished_run(self, invoice_id: str) -> str | None:
        """The run on this invoice that has not ended, or None."""
        return next(
            (
                run_id
                for run_id, run in self.runs.items()
                if run["invoice_id"] == invoice_id and not run["ended"]
            ),
            None,
        )

    def finish_run(
        self, run_id: str, *, step_count: int, last_step: str | None, last_state: InvoiceState
    ) -> None:
        """Note how it ended."""
        self.runs[run_id].update(
            ended=True, step_count=step_count, last_step=last_step, last_state=last_state
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
        """Note the readings."""
        self.extractions.append(
            {
                "invoice_id": invoice_id,
                "vision_ref": vision_ref,
                "text_ref": text_ref,
                "confidence_ref": confidence_ref,
                "auto_ok": auto_ok,
            }
        )

    def prior_invoice_count(self, vendor_id: str, *, excluding: str) -> int:
        """Other stored invoices with this vendor."""
        return sum(
            1
            for invoice_id, record in self.records.items()
            if record.vendor_id == vendor_id and invoice_id != excluding
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


@dataclass
class _StoredApproval:
    view: ApprovalView
    cancelled_by: str | None = None


@dataclass
class InMemoryApprovalStore:
    """An approval store that lives as long as the process. For tests."""

    requests: dict[str, _StoredApproval] = field(default_factory=dict[str, _StoredApproval])
    decisions: list[tuple[str, str, str, str]] = field(
        default_factory=list[tuple[str, str, str, str]]
    )
    """``(approval_id, user_id, decision, reason)`` in the order they were made."""

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
        """Open the request, or return the existing one under this id."""
        del amount, currency, config_version
        if approval_id in self.requests:
            return self.requests[approval_id].view, False
        from ap_agent.contracts.audit import utc_now  # noqa: PLC0415 - avoids a cycle at import

        view = ApprovalView(
            approval_id=approval_id,
            invoice_id=invoice_id,
            requested_at=utc_now(),
            required_approvers=requirement.required_approvers,
            approver_role=requirement.approver_role,
            status="pending",
            approvals=(),
        )
        self.requests[approval_id] = _StoredApproval(view)
        return view, True

    def get(self, approval_id: str) -> ApprovalView | None:
        """The request, or None."""
        stored = self.requests.get(approval_id)
        return stored.view if stored else None

    def decide(
        self, approval_id: str, *, user_id: str, decision: Literal["approve", "reject"], reason: str
    ) -> ApprovalView:
        """Record the decision; the status follows from all decisions so far."""
        view = self.requests[approval_id].view
        if view.status != "pending":
            msg = f"approval {approval_id} is {view.status}, not pending"
            raise ApprovalDecisionRefusedError(msg)
        if user_id in view.approvals or view.rejected_by == user_id:
            msg = f"{user_id} has already decided approval {approval_id}"
            raise ApprovalDecisionRefusedError(msg)
        self.decisions.append((approval_id, user_id, decision, reason))
        approvals = (*view.approvals, user_id) if decision == "approve" else view.approvals
        rejected_by = user_id if decision == "reject" else None
        status = outcome(view.required_approvers, len(approvals), rejected_by is not None)
        updated = replace(view, approvals=approvals, rejected_by=rejected_by, status=status)
        self.requests[approval_id] = _StoredApproval(updated)
        return updated

    def cancel(self, approval_id: str, *, by: str, reason: str) -> None:
        """Withdraw a pending request."""
        del reason
        stored = self.requests[approval_id]
        if stored.view.status == "pending":
            view = replace(stored.view, status="cancelled")
            self.requests[approval_id] = _StoredApproval(view, cancelled_by=by)
