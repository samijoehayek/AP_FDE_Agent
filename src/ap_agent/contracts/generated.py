"""What a generated invoice says, and what the pipeline is expected to do with it.

``scripts/generate_invoices.py`` renders an invoice PDF from a seeded purchase
order and writes one of these beside it. The PDF without the label is just a
PDF: a document nobody has checked is not a fixture, it is a sample.

This is the *only* half of the golden set with a purchase order behind it. A
downloaded corpus can exercise extraction and nothing else, because there is no
PO to match a downloaded invoice against - so every assertion about tolerances,
reason codes and approval routing has to be made against a document this
repository generated on purpose.

Two properties are worth stating outright.

**The expected fields are a claim about the page, not a computation.** Each
variant plants one known defect and declares what that defect should cause.
Nothing here evaluates a tolerance or runs a match; ``expected_match`` and
``expected_reason_codes`` are what a later test asserts the matcher produced,
and a disagreement between this file and the matcher is exactly the finding the
golden set exists to surface.

**``hidden_text`` is a plant, never a reading.** It holds the adversarial string
the generator drew onto the page in white 4pt type, so a test can assert both
that the text layer carries it and that the vision read does not. It is written
by the generator, from a constant in the generator, and no pipeline code may
ever populate it from an extraction. Rule 2 of CLAUDE.md is unaffected: no
contract in this package has a bank-details *field*, the vendor master remains
the only authority for remittance, and the account number in the planted string
is a published test IBAN on a document whose entire purpose is to be caught.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field

from ap_agent.contracts.common import (
    CurrencyCode,
    Money,
    Quantity,
    StrictModel,
    TaxRate,
    UnitPrice,
)
from ap_agent.contracts.enums import ReasonCode

GENERATOR_VERSION = "gen_v1"
"""Bumped when a change would alter the bytes of an already-generated invoice.

Hashes are document identity in this system, so a layout or wording change
produces different documents rather than new versions of the same ones. The
version is on every truth file to say which generator drew the page.
"""


class GeneratedVariant(StrEnum):
    """The defect planted on one rendering of a purchase order.

    Six variants per PO: one honest invoice, four with a single deliberate
    numeric or structural defect, and one adversarial. Exactly one thing is
    wrong with each, because a fixture carrying two defects cannot tell you
    which of them a rule caught.
    """

    CLEAN = "clean"
    PRICE_PLUS_3PCT = "price_plus_3pct"
    QTY_OVER_RECEIVED = "qty_over_received"
    FREIGHT_SMALL = "freight_small"
    FREIGHT_LARGE = "freight_large"
    HIDDEN_TEXT = "hidden_text"

    @property
    def code(self) -> str:
        """The short form used in the invoice number.

        Every variant of one purchase order needs a distinct invoice number, or
        the ``(vendor_id, invoice_number)`` uniqueness control fires across the
        fixture and every run after the first looks like a duplicate. Short
        because an invoice number is a printed field, not a slug.
        """
        return _VARIANT_CODES[self]


_VARIANT_CODES: dict[GeneratedVariant, str] = {
    GeneratedVariant.CLEAN: "CLN",
    GeneratedVariant.PRICE_PLUS_3PCT: "P03",
    GeneratedVariant.QTY_OVER_RECEIVED: "QTY",
    GeneratedVariant.FREIGHT_SMALL: "FRS",
    GeneratedVariant.FREIGHT_LARGE: "FRL",
    GeneratedVariant.HIDDEN_TEXT: "HID",
}


class ExpectedMatch(StrEnum):
    """Whether the three-way match should clear or hold this invoice."""

    MATCHED = "MATCHED"
    EXCEPTION = "EXCEPTION"


class ExpectedLine(StrictModel):
    """One line as printed, shaped to build a ``LineItem``.

    Field names match :class:`~ap_agent.contracts.invoice.LineItem` exactly, so
    a test can construct one by ``LineItem(**line.model_dump())``. A truth file
    that needed a translation layer to reach the contract it describes would
    drift from it.

    ``unit`` is ``"EA"`` throughout and is *not* printed on the page. The seed
    manifest carries no unit of measure, ``LineItem.unit`` is required, and
    inventing a per-item unit to print would be a fact about the fixture that
    nothing in the fixture supports.
    """

    description: str = Field(min_length=1, max_length=500)
    quantity: Quantity
    unit: str = Field(default="EA", min_length=1, max_length=16)
    unit_price: UnitPrice
    extended_price: Money
    tax_rate: TaxRate = Field(default=Decimal(0))
    po_line_ref: str | None = Field(
        default=None,
        max_length=64,
        description="None on every line. The seed manifest has no PO line references, so "
        "matching joins on the item description - which is unique within a PO.",
    )


class ExpectedInvoice(StrictModel):
    """What the page says, shaped to build an ``InvoiceExtraction``.

    Field names match :class:`~ap_agent.contracts.invoice.InvoiceExtraction`, so
    ``InvoiceExtraction(**expected.model_dump())`` validates, and the arithmetic
    self-check on that contract passes with no flags. That is the assertion this
    model exists for: the generator computed the totals it printed the same way
    the contract checks them, so a truth file that fails the validator means the
    generator is wrong about its own page.
    """

    vendor_name: str = Field(min_length=1, max_length=200)
    invoice_number: str = Field(min_length=1, max_length=64)
    invoice_date: date
    due_date: date
    currency: CurrencyCode
    subtotal: Money
    tax_total: Money
    total: Money
    payment_terms: str = Field(min_length=1, max_length=120)
    po_references: list[str] = Field(max_length=50)
    line_items: list[ExpectedLine] = Field(min_length=1, max_length=500)


class GeneratedInvoiceTruth(StrictModel):
    """The label for one generated invoice PDF.

    Serialised to ``truth.json`` next to the document. Decimals serialise as
    strings and dates as ISO-8601, because a truth file that round-tripped
    through a float would disagree with the page it describes in the fifteenth
    decimal place - and the tolerance checks it exists to test are decided in
    the second.
    """

    variant: GeneratedVariant
    po_number: str = Field(min_length=1, max_length=64)
    vendor_id: str = Field(
        min_length=1,
        max_length=64,
        description="The vendor's QuickBooks id, from the seed manifest. Not read from the page.",
    )
    vendor_name: str = Field(min_length=1, max_length=200)
    vendor_country: str = Field(
        min_length=2,
        max_length=2,
        description="ISO-3166-1 alpha-2, from the vendor master. Load-bearing: it is what "
        "settles a DD/MM against a MM/DD date. Generated invoices print an unambiguous "
        "date so they never need it, which is precisely why it must be recorded - a "
        "fixture that cannot state the country cannot be used to test the rule that reads it.",
    )

    expected: ExpectedInvoice

    planted_defect: str | None = Field(
        default=None,
        max_length=300,
        description="One sentence naming what was done to this rendering. None for clean.",
    )
    expected_match: ExpectedMatch
    expected_reason_codes: list[ReasonCode] = Field(
        default_factory=list[ReasonCode],
        description="Reasons the match is expected to raise, from the existing closed "
        "vocabulary. Empty on a clean pass.",
    )
    expected_human_review: bool = Field(
        description="Whether this document must reach a person. Not the same question as "
        "expected_match: the hidden-text variant matches cleanly on the numbers and "
        "still must not post without review."
    )
    hidden_text: str | None = Field(
        default=None,
        max_length=300,
        description="DISPLAY AND ASSERTION ONLY. The exact string drawn on the page in white "
        "4pt type, set only on the hidden_text variant. Written by the generator from its "
        "own constant; never populated from a document reading.",
    )
    generator_version: Literal["gen_v1"] = GENERATOR_VERSION


__all__ = [
    "GENERATOR_VERSION",
    "ExpectedInvoice",
    "ExpectedLine",
    "ExpectedMatch",
    "GeneratedInvoiceTruth",
    "GeneratedVariant",
]
