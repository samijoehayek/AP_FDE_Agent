"""The second - and last - seat the LLM occupies: explaining an exception.

Given a ``MatchResult`` that deterministic code has already produced, a model
writes a short human summary and picks a resolver and a next action from closed
lists. It does not decide whether there is an exception, what the reason code
is, or whether anything gets paid. It makes a queue item legible to the person
who has to act on it.

``human_summary`` is prose written for an approver. It is display-only: no code
parses it, and no decision reads it.

**What the seat is shown is codes and numbers, never prose.** The three input
shapes below - :class:`HeaderNumbers`, :class:`PoSnapshot` and
:class:`VendorSummary` - exist so that the explanation seat can be given the
facts of an exception without being given the document. Each is built from the
full object by a ``from_...`` constructor that copies numbers, codes and ERP ids
and drops every string a vendor wrote: no description, no vendor name, no
payment terms, no remit-to block, no evidence snippet, no ``suspicious_text``.
The extraction seat reads untrusted text because it has to; this one has no
reason to, so it never does.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Self

from pydantic import Field

from ap_agent.contracts.common import (
    MAX_SUMMARY_CHARS,
    CurrencyCode,
    Money,
    Quantity,
    StrictModel,
    UnitPrice,
)
from ap_agent.contracts.enums import ReasonCode, SuggestedAction, SuggestedResolver
from ap_agent.contracts.purchase_order import PurchaseOrderStatus
from ap_agent.contracts.vendor import VendorMatchBasis

if TYPE_CHECKING:
    from ap_agent.contracts.invoice import InvoiceExtraction
    from ap_agent.contracts.purchase_order import PurchaseOrder
    from ap_agent.contracts.vendor import VendorMatch


class ExceptionClassification(StrictModel):
    """A model-written explanation of a deterministically-detected exception."""

    reason_code: ReasonCode = Field(
        description="Must be one the MatchResult already reported. The model is choosing which "
        "reason to lead with, not inventing one.",
    )
    suggested_resolver: SuggestedResolver
    human_summary: str = Field(
        min_length=1,
        max_length=MAX_SUMMARY_CHARS,
        description="Plain-language explanation for the approver's queue. Display only.",
    )
    suggested_action: SuggestedAction = Field(
        description="A routing hint for a human. Never dispatched to a tool.",
    )


class HeaderNumbers(StrictModel):
    """The invoice header, reduced to what can be counted.

    No invoice number: it is a string the vendor chose, the approver's queue
    already shows it, and the explanation does not need it to be right.
    """

    currency: CurrencyCode
    subtotal: Money
    tax_total: Money
    total: Money
    invoice_date: date
    due_date: date | None = None
    line_count: int = Field(ge=0)

    @classmethod
    def from_extraction(cls, extraction: InvoiceExtraction) -> Self:
        """Copy the numbers; leave every vendor-written string behind."""
        return cls(
            currency=extraction.currency,
            subtotal=extraction.subtotal,
            tax_total=extraction.tax_total,
            total=extraction.total,
            invoice_date=extraction.invoice_date,
            due_date=extraction.due_date,
            line_count=len(extraction.line_items),
        )


class PoLineSnapshot(StrictModel):
    """One ordered line, identified by number. No description."""

    line_no: int = Field(ge=1)
    qty_ordered: Quantity
    unit_price: UnitPrice
    extended: Money


class PoSnapshot(StrictModel):
    """The purchase order as the ERP holds it, without its prose.

    Line descriptions are left out even though they come from the buyer's ERP
    rather than the document: the match identifies lines by number, and keeping
    the seat on numbers alone is simpler to state and to test than deciding
    which strings are trusted.
    """

    po_number: str = Field(min_length=1, max_length=64)
    status: PurchaseOrderStatus
    currency: CurrencyCode
    lines: list[PoLineSnapshot] = Field(max_length=500)

    @classmethod
    def from_purchase_order(cls, order: PurchaseOrder) -> Self:
        """Copy the numbers and the line numbering; drop names and descriptions."""
        return cls(
            po_number=order.po_number,
            status=order.status,
            currency=order.currency,
            lines=[
                PoLineSnapshot(
                    line_no=line.line_no,
                    qty_ordered=line.qty_ordered,
                    unit_price=line.unit_price,
                    extended=line.extended,
                )
                for line in order.lines
            ],
        )


class VendorSummary(StrictModel):
    """What vendor resolution concluded, as codes. No name, no address."""

    vendor_id: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    currency: CurrencyCode | None = None
    match_basis: VendorMatchBasis
    remit_to_matches_master: bool | None = None

    @classmethod
    def from_match(cls, match: VendorMatch) -> Self:
        """Copy the verdict; leave the names behind."""
        return cls(
            vendor_id=match.vendor_id,
            country=match.country,
            currency=match.currency,
            match_basis=match.match_basis,
            remit_to_matches_master=match.remit_to_matches_master,
        )
