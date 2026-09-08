"""The result of matching an invoice to its purchase order and receipts.

Nothing in this module is produced by a model. A ``MatchResult`` is the output
of deterministic code reading a ``PurchaseOrder``, its receipts, and the
versioned tolerance config; a model may later be asked to *explain* one, and
that explanation is a separate contract
(:class:`~ap_agent.contracts.exceptions.ExceptionClassification`) so the two can
never be confused at a call site.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from ap_agent.contracts.common import Money, StrictModel
from ap_agent.contracts.enums import MatchLineStatus, MatchTotalsStatus, ReasonCode


class MatchLineResult(StrictModel):
    """How one invoice line fared against the PO and its receipts."""

    invoice_line_index: int = Field(
        ge=0, description="Index into InvoiceExtraction.line_items. Positional, stable per run."
    )
    po_number: str | None = Field(default=None, max_length=64)
    po_line_ref: str | None = Field(
        default=None,
        max_length=64,
        description="The PO line this was matched to by code - not the reference printed on "
        "the invoice.",
    )
    status: MatchLineStatus

    invoiced_quantity: Decimal | None = None
    ordered_quantity: Decimal | None = None
    received_quantity: Decimal | None = None

    invoiced_unit_price: Decimal | None = None
    po_unit_price: Decimal | None = None

    price_variance_pct: Decimal | None = Field(
        default=None,
        description="Signed fraction: 0.05 means the invoice is 5% above the PO price.",
    )
    quantity_variance_pct: Decimal | None = Field(
        default=None, description="Signed fraction against the received quantity."
    )

    reason_codes: list[ReasonCode] = Field(default_factory=list[ReasonCode])


class MatchResult(StrictModel):
    """The full match outcome for one invoice."""

    invoice_id: str = Field(min_length=1, max_length=64)
    po_numbers: list[str] = Field(
        default_factory=list[str],
        max_length=50,
        description="POs actually resolved, not merely cited.",
    )

    line_results: list[MatchLineResult] = Field(default_factory=list[MatchLineResult])
    totals_status: MatchTotalsStatus

    invoiced_total: Money | None = None
    po_total: Money | None = None
    totals_variance_pct: Decimal | None = None

    reason_codes: list[ReasonCode] = Field(
        default_factory=list[ReasonCode],
        description="Invoice-level reasons. Line-level reasons stay on their line so an "
        "approver can see which line caused the hold.",
    )
    config_version: str = Field(
        min_length=1,
        max_length=32,
        description="Version of the guardrails config that produced this result. An audited "
        "decision must name the ruleset that made it.",
    )

    @property
    def is_clean(self) -> bool:
        """True when nothing needs a human: no reason codes and totals in tolerance."""
        return (
            self.totals_status is MatchTotalsStatus.OK
            and not self.reason_codes
            and all(line.status is MatchLineStatus.OK for line in self.line_results)
        )
