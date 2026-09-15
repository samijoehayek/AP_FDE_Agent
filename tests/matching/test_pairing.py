"""Which invoice line is billing which purchase-order line.

Every comparison the matcher makes afterwards rests on this, so the cases worth
writing are the ones where a lazier implementation would look right: a line
billed twice, a line ordered and not billed, and a description that could mean
two things.

Pure functions, no fixtures on disk, no I/O.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ap_agent.contracts.invoice import LineItem
from ap_agent.contracts.purchase_order import PurchaseOrderLine
from ap_agent.matching.pairing import normalise_description, pair_lines


def _po(line_no: int, description: str | None, item_ref: str | None = None) -> PurchaseOrderLine:
    return PurchaseOrderLine(
        line_no=line_no,
        item_ref=item_ref,
        description=description,
        qty_ordered=Decimal(10),
        unit_price=Decimal("5.00"),
        extended=Decimal("50.00"),
    )


def _invoice(description: str, po_line_ref: str | None = None, qty: int = 1) -> LineItem:
    return LineItem(
        description=description,
        quantity=Decimal(qty),
        unit_price=Decimal("5.00"),
        extended_price=Decimal(qty) * Decimal("5.00"),
        po_line_ref=po_line_ref,
    )


# --- the reference the document printed --------------------------------------


def test_a_printed_reference_pairs_the_line() -> None:
    """Better evidence than a string comparison: the vendor is telling you."""
    po_lines = [_po(1, "Laser Printer Mono", item_ref="23"), _po(2, "27in IPS Monitor", "20")]
    invoice_lines = [_invoice("something else entirely", po_line_ref="20")]

    paired = pair_lines(invoice_lines, po_lines)

    assert len(paired.pairs) == 1
    assert paired.pairs[0].po_line.line_no == 2
    assert paired.unmatched == ()


def test_the_reference_wins_over_the_description() -> None:
    """A description can coincide; a reference is a claim about which line."""
    po_lines = [_po(1, "Widget", item_ref="A"), _po(2, "Gasket", item_ref="B")]
    invoice_lines = [_invoice("Widget", po_line_ref="B")]

    paired = pair_lines(invoice_lines, po_lines)

    assert paired.pairs[0].po_line.line_no == 2


def test_a_reference_that_matches_nothing_falls_through_to_the_description() -> None:
    """A claim that turned out to be wrong is not a reason to give up on the line."""
    po_lines = [_po(1, "Widget", item_ref="A")]
    invoice_lines = [_invoice("Widget", po_line_ref="NOT-A-REAL-REF")]

    paired = pair_lines(invoice_lines, po_lines)

    assert len(paired.pairs) == 1
    assert paired.pairs[0].po_line.line_no == 1


# --- the description ---------------------------------------------------------


def test_an_exact_description_pairs_the_line() -> None:
    po_lines = [_po(1, "Laser Printer Mono"), _po(2, "27in IPS Monitor")]
    invoice_lines = [_invoice("27in IPS Monitor")]

    paired = pair_lines(invoice_lines, po_lines)

    assert paired.pairs[0].po_line.line_no == 2


@pytest.mark.parametrize(
    "printed",
    [
        "A4 Copier Paper (box)",
        "a4 copier paper box",
        "A4  COPIER   PAPER, BOX",
        "  A4 Copier Paper (Box)  ",
    ],
)
def test_a_reworded_description_still_pairs(printed: str) -> None:
    """Case, punctuation and spacing are rendering, not identity."""
    po_lines = [_po(1, "A4 Copier Paper (box)")]

    paired = pair_lines([_invoice(printed)], po_lines)

    assert len(paired.pairs) == 1
    assert paired.unmatched == ()


def test_a_description_matching_nothing_is_unmatched() -> None:
    """Freight is the ordinary case: on the invoice, on no order line."""
    po_lines = [_po(1, "Widget")]
    invoice_lines = [_invoice("Widget"), _invoice("Freight")]

    paired = pair_lines(invoice_lines, po_lines)

    assert len(paired.pairs) == 1
    assert [item.line.description for item in paired.unmatched] == ["Freight"]
    assert paired.unmatched[0].index == 1


def test_an_ambiguous_description_pairs_nothing() -> None:
    """A key that identifies two lines identifies neither.

    The line falls through to unmatched, where the unmatched-charge tolerance
    sends it to a person - which is the direction a matcher should fail in.
    """
    po_lines = [_po(1, "Widget"), _po(2, "widget")]

    paired = pair_lines([_invoice("Widget")], po_lines)

    assert paired.pairs == ()
    assert len(paired.unmatched) == 1
    assert len(paired.unbilled) == 2


def test_a_po_line_with_no_description_is_not_a_key() -> None:
    """QuickBooks does not require one, and an empty key would collide with itself."""
    po_lines = [_po(1, None), _po(2, "Widget")]

    paired = pair_lines([_invoice("Widget")], po_lines)

    assert paired.pairs[0].po_line.line_no == 2
    assert [line.line_no for line in paired.unbilled] == [1]


# --- the cases a lazier implementation gets wrong -----------------------------


def test_two_invoice_lines_can_bill_one_po_line() -> None:
    """The purchase-order line is not consumed by the first pairing.

    This is the case the whole design turns on. Two lines of 2 against a receipt
    of 3 look fine apart and over-bill together, and a matcher that dropped the
    order line after its first pair would never see it.
    """
    po_lines = [_po(1, "Widget")]
    invoice_lines = [_invoice("Widget", qty=2), _invoice("Widget", qty=2)]

    paired = pair_lines(invoice_lines, po_lines)

    assert len(paired.pairs) == 2
    assert {pair.po_line.line_no for pair in paired.pairs} == {1}
    assert [pair.invoice.index for pair in paired.pairs] == [0, 1]
    assert paired.unmatched == ()
    assert paired.unbilled == (), "a line billed twice is still billed"


def test_one_invoice_line_never_pairs_to_two_po_lines() -> None:
    """The reverse of the rule above, and it is not symmetric."""
    po_lines = [_po(1, "Widget", item_ref="A"), _po(2, "Gasket", item_ref="B")]

    paired = pair_lines([_invoice("Widget", po_line_ref="A")], po_lines)

    assert len(paired.pairs) == 1


def test_an_ordered_line_nobody_billed_is_unbilled_not_unmatched() -> None:
    """A partial invoice against a partial delivery is normal, not an exception."""
    po_lines = [_po(1, "Widget"), _po(2, "Gasket"), _po(3, "Bracket")]

    paired = pair_lines([_invoice("Widget")], po_lines)

    assert len(paired.pairs) == 1
    assert paired.unmatched == ()
    assert [line.line_no for line in paired.unbilled] == [2, 3]


# --- shape ------------------------------------------------------------------


def test_document_order_is_preserved() -> None:
    """The result identifies invoice lines by position, so position must hold."""
    po_lines = [_po(1, "Widget"), _po(2, "Gasket")]
    invoice_lines = [_invoice("Gasket"), _invoice("Freight"), _invoice("Widget")]

    paired = pair_lines(invoice_lines, po_lines)

    assert [pair.invoice.index for pair in paired.pairs] == [0, 2]
    assert [item.index for item in paired.unmatched] == [1]


def test_an_empty_invoice_leaves_every_line_unbilled() -> None:
    po_lines = [_po(1, "Widget"), _po(2, "Gasket")]

    paired = pair_lines([], po_lines)

    assert paired.pairs == ()
    assert paired.unmatched == ()
    assert len(paired.unbilled) == 2


def test_an_order_with_no_lines_leaves_everything_unmatched() -> None:
    paired = pair_lines([_invoice("Widget")], [])

    assert paired.pairs == ()
    assert len(paired.unmatched) == 1
    assert paired.unbilled == ()


def test_nothing_here_reads_a_price_or_a_quantity() -> None:
    """Pairing answers "which lines go together", not "do they agree".

    Same pairing whatever the numbers are - asserted rather than assumed, because
    a tolerance leaking into this function would make the match depend on the
    order lines happened to be compared in.
    """
    po_lines = [_po(1, "Widget")]
    cheap = pair_lines([_invoice("Widget", qty=1)], po_lines)
    expensive = pair_lines([_invoice("Widget", qty=9999)], po_lines)

    assert cheap.pairs[0].po_line.line_no == expensive.pairs[0].po_line.line_no


def test_normalisation_is_the_vendor_normaliser() -> None:
    """Shared on purpose: two implementations would drift, and about money."""
    assert normalise_description("A4 Copier Paper (box)") == "a4 copier paper box"
    assert normalise_description("  Widget,  10mm ") == "widget 10mm"
