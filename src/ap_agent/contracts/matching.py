"""The result of matching an invoice to its purchase order and receipts.

Nothing in this module is produced by a model. A ``MatchResult`` is the output of
deterministic code reading a ``PurchaseOrder``, its receipts, and the versioned
tolerance config; a model may later be asked to *explain* one, and that
explanation is a separate contract
(:class:`~ap_agent.contracts.exceptions.ExceptionClassification`) so the two can
never be confused at a call site.

**There is no free-text field here, and that is a control rather than an
oversight.** The matcher never reads ``suspicious_text``, ``remit_to_display``,
or a line description as anything but a join key, and it writes no prose. So
there is nowhere in this object for document content to travel into a decision,
and nowhere for a later reader to mistake something a vendor typed for something
this system concluded.

Every number a reviewer would want to check the arithmetic with is on the
result - both unit prices, both quantities, the variance computed each way, and
the three header deltas. That is deliberate: an exception a person cannot
re-derive from the row in front of them is an exception they have to take on
trust, and a review queue built on trust is a review queue people click through.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import Field

from ap_agent.contracts.common import Money, Quantity, StrictModel, UnitPrice
from ap_agent.contracts.enums import MatchLineOutcome, ReasonCode

SignedPercentage = Annotated[
    Decimal,
    Field(
        max_digits=9,
        decimal_places=4,
        allow_inf_nan=False,
        description="A signed percentage: 3.0 means the invoice is 3% above the purchase "
        "order, -1.5 means it is 1.5% below.",
    ),
]
"""Signed on purpose. Under-charging is not an exception, but it is worth seeing."""


class MatchLine(StrictModel):
    """What the matcher decided about one line, with the numbers behind it.

    The identifiers are both optional because a line can exist on one side only.
    ``line_no`` is the purchase-order line and is ``None`` for a charge the order
    does not cover; ``invoice_line_index`` is the position in
    ``InvoiceExtraction.line_items`` and is ``None`` for an ordered line nobody
    billed. Exactly one of them is ``None`` on an unpaired line, and neither is
    on a pair.

    No description. It would be the one field here a vendor controls, and a
    result object is not a place for document content - the indices identify the
    lines without carrying anything typed by whoever made the invoice.
    """

    outcome: MatchLineOutcome
    line_no: int | None = Field(
        default=None, ge=1, description="The purchase-order line. None when nothing matched."
    )
    invoice_line_index: int | None = Field(
        default=None,
        ge=0,
        description="Index into InvoiceExtraction.line_items. None for an unbilled PO line.",
    )

    invoice_qty: Quantity | None = Field(
        default=None,
        description="Summed across every invoice line paired to this purchase-order line, "
        "because two lines billing the same order line over-bill it together.",
    )
    received_qty: Quantity | None = Field(
        default=None,
        description="What the receipts record for this line. Zero and None are different "
        "answers: zero is ordered-and-nothing-came, None is no receipt line at all.",
    )

    po_unit_price: UnitPrice | None = None
    invoice_unit_price: UnitPrice | None = None
    price_variance_pct: SignedPercentage | None = None
    price_variance_abs: Money | None = Field(
        default=None, description="Signed, per unit, in the invoice's currency."
    )


class MatchResult(StrictModel):
    """The full match outcome for one invoice against one purchase order.

    ``matched`` is true if and only if ``reason_codes`` is empty. The two are not
    independent judgements, and stating it that way means nothing can report a
    clean match while carrying a reason a human should see.

    ``config_version`` names the ruleset that produced this. A decision made last
    quarter has to stay re-explainable under last quarter's tolerances, and it
    only can if the result says which file decided it.
    """

    matched: bool = Field(
        description="True iff reason_codes is empty. A match is the absence of reasons."
    )
    reason_codes: list[ReasonCode] = Field(
        default_factory=list[ReasonCode],
        max_length=50,
        description="Every reason this invoice needs a person, from the closed vocabulary. "
        "Deduplicated and ordered as found, so the first is the one the matcher hit first.",
    )
    config_version: str = Field(
        min_length=1,
        max_length=32,
        description="The guardrails version whose tolerances produced this result.",
    )
    po_number: str = Field(
        min_length=1, max_length=64, description="The order this invoice was matched against."
    )

    lines: list[MatchLine] = Field(
        default_factory=list[MatchLine],
        max_length=1000,
        description="Every line on either side: pairs, unmatched charges, and unbilled "
        "order lines. Long enough to hold both sides of a 500-line invoice.",
    )

    subtotal_delta: Money = Field(
        default=Decimal(0),
        description="Invoice subtotal minus the sum of its own lines. Signed.",
    )
    tax_delta: Money = Field(
        default=Decimal(0), description="Invoice tax minus the tax its lines imply. Signed."
    )
    total_delta: Money = Field(
        default=Decimal(0),
        description="Invoice total minus subtotal plus tax. Signed. Rounding lives here, "
        "and nothing else should.",
    )
