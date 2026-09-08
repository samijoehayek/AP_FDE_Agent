"""Find invoices that may already have been received or paid.

Caller: code. The orchestrator calls this; no model can.
Side effects: DB_READ, ERP_READ.

Read-only. Duplicate payment is the most common way an AP function loses
money without anyone doing anything wrong, and it is a pure data-matching
problem - which is why no model is anywhere near it.

Not implemented: the candidate keys (exact document hash; vendor + invoice
number; vendor + amount + date window; vendor + amount + fuzzy number for
re-issued invoices) and their scoring.

"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.DB_READ, SideEffect.ERP_READ)
REQUIRES_IDEMPOTENCY_KEY = False


class DuplicateCandidate(ToolOutput):
    """A prior invoice that resembles the one being checked."""

    invoice_id: str = Field(min_length=1, max_length=64)
    matched_on: list[str] = Field(
        min_length=1, description="Which keys matched, e.g. ['vendor_id', 'total', 'date_window']."
    )
    score: float = Field(ge=0.0, le=1.0)
    already_paid: bool = Field(
        description="A duplicate of an unpaid invoice is a filing problem; a duplicate of a "
        "paid one is a loss. They route differently."
    )


class FindDuplicatesInput(ToolInput):
    """Input for :func:`find_duplicates`."""

    vendor_id: str = Field(min_length=1, max_length=64)
    invoice_number: str = Field(min_length=1, max_length=64)
    invoice_date: date
    total: Money
    currency: CurrencyCode
    document_sha256: str = Field(min_length=64, max_length=64)
    exclude_invoice_id: str | None = Field(
        default=None, description="The invoice being checked, so it does not match itself."
    )


class FindDuplicatesOutput(ToolOutput):
    """Output of :func:`find_duplicates`."""

    matches: list[DuplicateCandidate] = Field(
        default_factory=list[DuplicateCandidate], max_length=25
    )
    highest_score: float = Field(default=0.0, ge=0.0, le=1.0)


def find_duplicates(payload: FindDuplicatesInput) -> FindDuplicatesOutput:
    """Find invoices that may already have been received or paid.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
