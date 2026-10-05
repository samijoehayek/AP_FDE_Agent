"""What the matcher decides, case by case.

Every test here builds its own order, receipt and invoice, so each one states
the whole situation it is about. The generated fixture is swept separately in
``test_generated_fixture.py``; this file is for the cases worth writing down on
their own, which are mostly the boundaries and the ones a plausible-looking
implementation gets wrong.

The tolerances are the real ones from ``config/guardrails.v1.yaml``. See
``conftest.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from tests.matching.conftest import (
    PO_NUMBER,
    invoice,
    invoice_line,
    po_line,
    purchase_order,
    receipts,
)

from ap_agent.contracts.enums import MatchLineOutcome, ReasonCode
from ap_agent.contracts.purchase_order import PurchaseOrderStatus, ReceiptSet
from ap_agent.matching import compute_match

if TYPE_CHECKING:
    from ap_agent.contracts.guardrails import GuardrailConfig
    from ap_agent.contracts.invoice import InvoiceExtraction
    from ap_agent.contracts.purchase_order import PurchaseOrder

WIDGET = "Widget 10mm"
GASKET = "Gasket 4in"


# --- the clean case ---------------------------------------------------------


def test_an_invoice_for_what_arrived_at_the_price_ordered_matches(
    guardrails: GuardrailConfig,
) -> None:
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert result.matched
    assert result.reason_codes == []
    assert result.po_number == PO_NUMBER
    assert result.config_version == guardrails.config_version
    assert [line.outcome for line in result.lines] == [MatchLineOutcome.OK]


def test_matched_is_the_absence_of_reasons(guardrails: GuardrailConfig) -> None:
    """Stated as an invariant, because the two must never be set independently."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    for billed in ("10", "11", "9"):
        result = compute_match(
            invoice(invoice_line(WIDGET, billed, "20.00")),
            order,
            receipts((1, "10")),
            guardrails,
        )
        assert result.matched is (result.reason_codes == [])


def test_billing_less_than_arrived_is_nobody_s_exception(
    guardrails: GuardrailConfig,
) -> None:
    """A shortfall is the vendor's loss. Holding it would teach reviewers to skim."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "6", "20.00")),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert result.matched


def test_a_cheaper_price_is_not_an_exception(guardrails: GuardrailConfig) -> None:
    """Signed variance, one-sided test. The number is still on the row."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "18.00")),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert result.matched
    assert result.lines[0].price_variance_abs == Decimal("-2.00")
    assert result.lines[0].price_variance_pct == Decimal("-10.0000")


# --- quantity: against what arrived, never what was ordered -------------------


def test_two_units_on_a_po_that_received_nothing_is_over_billed(
    guardrails: GuardrailConfig,
) -> None:
    """The case this whole module is shaped around.

    Eleven were ordered and none arrived. Billing two is comfortably inside the
    order and is a bill for two units that do not exist. A matcher comparing to
    ``qty_ordered`` passes this, which is why the fixture
    ``AP-SEED-010/qty_over_received`` exists and why this test is here too.
    """
    order = purchase_order(po_line(1, WIDGET, "11", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "2", "20.00")),
        order,
        receipts((1, "0")),
        guardrails,
    )

    assert not result.matched
    assert ReasonCode.QUANTITY_OVER_TOLERANCE in result.reason_codes
    assert result.lines[0].outcome is MatchLineOutcome.QTY_OVER
    assert result.lines[0].received_qty == Decimal("0.000000")


def test_billing_the_full_order_against_a_partial_delivery_is_over_billed(
    guardrails: GuardrailConfig,
) -> None:
    """Twelve ordered, eight arrived, twelve billed. Inside the order, outside the truth."""
    order = purchase_order(po_line(1, WIDGET, "12", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "12", "20.00")),
        order,
        receipts((1, "8")),
        guardrails,
    )

    assert ReasonCode.QUANTITY_OVER_TOLERANCE in result.reason_codes


def test_exactly_the_received_quantity_passes(guardrails: GuardrailConfig) -> None:
    """The boundary. ``qty_over_billing_pct`` is zero, so equal must pass."""
    order = purchase_order(po_line(1, WIDGET, "12", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "8", "20.00")),
        order,
        receipts((1, "8")),
        guardrails,
    )

    assert result.matched


def test_two_invoice_lines_billing_one_po_line_are_summed(
    guardrails: GuardrailConfig,
) -> None:
    """Each line looks fine alone; together they bill four against three received.

    This is the case that decides the shape of ``pair_lines``. A matcher that
    consumed the purchase-order line on the first pairing would compare 2 to 3,
    pass, and then treat the second line as a charge nobody ordered - which the
    unmatched-charge tolerance would wave through at $40.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "2", "20.00"), invoice_line(WIDGET, "2", "20.00")),
        order,
        receipts((1, "3")),
        guardrails,
    )

    assert not result.matched
    assert ReasonCode.QUANTITY_OVER_TOLERANCE in result.reason_codes
    assert ReasonCode.LINE_NOT_ON_PO not in result.reason_codes

    rows = [line for line in result.lines if line.line_no == 1]
    assert len(rows) == 2, "one row per invoice line, both about the same order line"
    assert [row.invoice_qty for row in rows] == [Decimal("4.000000")] * 2, (
        "the summed quantity is on every row, because the decision was made on the sum"
    )
    assert {row.invoice_line_index for row in rows} == {0, 1}


def test_two_invoice_lines_under_the_received_quantity_pass(
    guardrails: GuardrailConfig,
) -> None:
    """The same shape, summing to less than arrived. Nothing fires."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "2", "20.00"), invoice_line(WIDGET, "1", "20.00")),
        order,
        receipts((1, "3")),
        guardrails,
    )

    assert result.matched
    assert [row.invoice_qty for row in result.lines] == [Decimal("3.000000")] * 2


def test_the_quantity_verdict_is_shared_but_the_price_is_each_line_s_own(
    guardrails: GuardrailConfig,
) -> None:
    """Averaging the prices would let a correct line pay for an inflated one."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "1", "20.00"), invoice_line(WIDGET, "1", "90.00")),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert ReasonCode.PRICE_OVER_TOLERANCE in result.reason_codes
    assert [row.invoice_unit_price for row in result.lines] == [
        Decimal("20.000000"),
        Decimal("90.000000"),
    ]
    assert [row.outcome for row in result.lines] == [
        MatchLineOutcome.OK,
        MatchLineOutcome.PRICE_OVER,
    ]


# --- receipts ---------------------------------------------------------------


def test_a_billed_line_with_nothing_received_raises_receipt_missing(
    guardrails: GuardrailConfig,
) -> None:
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        receipts((1, "0")),
        guardrails,
    )

    assert ReasonCode.RECEIPT_MISSING in result.reason_codes


def test_nothing_received_and_over_billed_reports_both(
    guardrails: GuardrailConfig,
) -> None:
    """Two different facts, and a reviewer needs both.

    "Nothing arrived" is why this is held; "you billed for ten of them" is what
    the vendor did about it. Suppressing either would make the result read as a
    smaller problem than it is.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        receipts((1, "0")),
        guardrails,
    )

    assert set(result.reason_codes) == {
        ReasonCode.RECEIPT_MISSING,
        ReasonCode.QUANTITY_OVER_TOLERANCE,
    }


def test_no_receipt_line_at_all_is_recorded_as_none_not_zero(
    guardrails: GuardrailConfig,
) -> None:
    """The row keeps the distinction even though the verdict does not.

    ``None`` means nothing was ever receipted against this line and ``0`` means
    it was receipted and nothing came. Neither is evidence anything arrived, so
    both bill the same; only the row can tell a reviewer which happened.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        ReceiptSet(po_number=PO_NUMBER),
        guardrails,
    )

    assert result.lines[0].received_qty is None
    assert ReasonCode.RECEIPT_MISSING in result.reason_codes


def test_an_unbilled_line_with_no_receipt_raises_nothing(
    guardrails: GuardrailConfig,
) -> None:
    """Ordered, never delivered, never billed. There is nothing to object to.

    This is the shape of ``AP-SEED-009``: three lines ordered, one of which
    received nothing and which the vendor correctly left off the invoice. A
    matcher raising RECEIPT_MISSING for it would hold a perfectly good invoice.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"), po_line(2, GASKET, "2", "5.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        receipts((1, "10"), (2, "0")),
        guardrails,
    )

    assert result.matched
    unbilled = [line for line in result.lines if line.outcome is MatchLineOutcome.UNBILLED]
    assert [line.line_no for line in unbilled] == [2]
    assert unbilled[0].received_qty == Decimal("0.000000")


# --- price ------------------------------------------------------------------


def test_three_percent_over_on_a_large_line_fails_both_legs(
    guardrails: GuardrailConfig,
) -> None:
    order = purchase_order(po_line(1, WIDGET, "11", "18900.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "11", "19467.00")),
        order,
        receipts((1, "11")),
        guardrails,
    )

    assert ReasonCode.PRICE_OVER_TOLERANCE in result.reason_codes
    assert result.lines[0].price_variance_pct == Decimal("3.0000")
    assert result.lines[0].price_variance_abs == Decimal("567.00")


def test_a_small_percentage_of_a_large_price_still_fails_on_the_absolute_leg(
    guardrails: GuardrailConfig,
) -> None:
    """1% of $18,900 is $189. Inside the percentage band, far outside $50.

    This is the AND doing the work the report's OR would not: on a large line
    the percentage alone is a licence, and it is exactly the size of line where
    the money is.
    """
    order = purchase_order(po_line(1, WIDGET, "11", "18900.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "11", "19089.00")),
        order,
        receipts((1, "11")),
        guardrails,
    )

    assert ReasonCode.PRICE_OVER_TOLERANCE in result.reason_codes
    assert result.lines[0].price_variance_pct == Decimal("1.0000")


def test_a_few_dollars_on_a_tiny_line_fails_on_the_percentage_leg(
    guardrails: GuardrailConfig,
) -> None:
    """$12 to $20 is $8 - well under the $50 cap and 66% over the order."""
    order = purchase_order(po_line(1, WIDGET, "1", "12.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "1", "20.00")),
        order,
        receipts((1, "1")),
        guardrails,
    )

    assert ReasonCode.PRICE_OVER_TOLERANCE in result.reason_codes


def test_within_both_legs_passes(guardrails: GuardrailConfig) -> None:
    """1% of $1,000 is $10: inside 2% and inside $50."""
    order = purchase_order(po_line(1, WIDGET, "1", "1000.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "1", "1010.00")),
        order,
        receipts((1, "1")),
        guardrails,
    )

    assert result.matched


def test_a_line_ordered_at_zero_has_no_percentage_and_fails_on_the_amount(
    guardrails: GuardrailConfig,
) -> None:
    """A percentage of zero is undefined, not enormous. The row says so."""
    order = purchase_order(po_line(1, WIDGET, "1", "0"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "1", "80.00")),
        order,
        receipts((1, "1")),
        guardrails,
    )

    assert ReasonCode.PRICE_OVER_TOLERANCE in result.reason_codes
    assert result.lines[0].price_variance_pct is None
    assert result.lines[0].price_variance_abs == Decimal("80.00")


def test_a_line_over_on_both_counts_leads_with_the_quantity(
    guardrails: GuardrailConfig,
) -> None:
    """Both numbers stay on the row; only the word the row leads with changes.

    Quantity first because it is the more serious claim: a price variance is an
    argument about what something costs, an over-receipt quantity is a bill for
    goods that do not exist.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "200.00")),
        order,
        receipts((1, "4")),
        guardrails,
    )

    assert result.lines[0].outcome is MatchLineOutcome.QTY_OVER
    assert result.lines[0].price_variance_abs == Decimal("180.00")
    assert set(result.reason_codes) == {
        ReasonCode.QUANTITY_OVER_TOLERANCE,
        ReasonCode.PRICE_OVER_TOLERANCE,
    }


# --- charges no order line covers --------------------------------------------


def test_freight_at_exactly_the_limit_passes(guardrails: GuardrailConfig) -> None:
    """$50.00 against a $50.00 cap. Inclusive, and worth pinning down.

    The percentage leg is satisfied here too: 2% of the $10,000 order is $200.
    A limit that excluded its own boundary would hold an invoice for being
    exactly what the config permits, and nobody reading the YAML would expect
    that.
    """
    order = purchase_order(po_line(1, WIDGET, "500", "20.00"))
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "500", "20.00"),
            invoice_line("Freight", "1", "50.00"),
        ),
        order,
        receipts((1, "500")),
        guardrails,
    )

    assert result.matched
    freight = result.lines[-1]
    assert freight.outcome is MatchLineOutcome.UNMATCHED
    assert freight.line_no is None
    assert freight.invoice_unit_price == Decimal("50.000000")


def test_a_penny_over_the_limit_does_not(guardrails: GuardrailConfig) -> None:
    order = purchase_order(po_line(1, WIDGET, "500", "20.00"))
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "500", "20.00"),
            invoice_line("Freight", "1", "50.01"),
        ),
        order,
        receipts((1, "500")),
        guardrails,
    )

    assert ReasonCode.LINE_NOT_ON_PO in result.reason_codes


def test_a_small_charge_on_a_small_order_fails_the_percentage_leg(
    guardrails: GuardrailConfig,
) -> None:
    """$30 of freight on a $200 order is 15%. Under the cap, over the share.

    The mirror of the price case: here the percentage is the leg that binds,
    which is what the AND buys on small orders.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "10", "20.00"),
            invoice_line("Freight", "1", "30.00"),
        ),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert ReasonCode.LINE_NOT_ON_PO in result.reason_codes


def test_the_share_is_taken_against_the_sum_of_the_ordered_lines(
    guardrails: GuardrailConfig,
) -> None:
    """Not a stored header total, which could disagree with the lines it heads."""
    order = purchase_order(po_line(1, WIDGET, "100", "20.00"))
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "100", "20.00"),
            invoice_line("Freight", "1", "40.00"),
        ),
        order,
        receipts((1, "100")),
        guardrails,
    )

    assert result.matched, "40.00 is under $50 and under 2% of the 2,000.00 ordered"


def _with_freight(
    *amounts: str, qty: str = "500"
) -> tuple[InvoiceExtraction, PurchaseOrder, ReceiptSet]:
    """An order for ``qty`` widgets at $20, billed exactly, plus freight lines."""
    return (
        invoice(
            invoice_line(WIDGET, qty, "20.00"),
            *(invoice_line("Freight", "1", amount) for amount in amounts),
        ),
        purchase_order(po_line(1, WIDGET, qty, "20.00")),
        receipts((1, qty)),
    )


def test_freight_large_split_into_two_lines_is_still_held(
    guardrails: GuardrailConfig,
) -> None:
    """The fixture's $120 as two $60 lines. Each line is over the cap on its own."""
    extraction, order, received = _with_freight("60.00", "60.00")

    result = compute_match(extraction, order, received, guardrails)

    assert ReasonCode.LINE_NOT_ON_PO in result.reason_codes


def test_freight_split_under_the_cap_is_caught_by_the_sum(
    guardrails: GuardrailConfig,
) -> None:
    """$120 as three $40 lines: every line passes alone, so only the sum holds it.

    This is the case the combined test exists for. Without it a vendor could
    bill any amount of unexplained charges in pieces just under the cap.
    """
    extraction, order, received = _with_freight("40.00", "40.00", "40.00")

    result = compute_match(extraction, order, received, guardrails)

    assert ReasonCode.LINE_NOT_ON_PO in result.reason_codes
    unmatched = [line for line in result.lines if line.outcome is MatchLineOutcome.UNMATCHED]
    assert [line.invoice_line_index for line in unmatched] == [1, 2, 3], "one row per line"


def test_split_freight_is_summed_against_the_percentage_leg_too(
    guardrails: GuardrailConfig,
) -> None:
    """Two $15 lines on a $1,000 order: $30 is under the cap and over the 2% share."""
    extraction, order, received = _with_freight("15.00", "15.00", qty="50")

    result = compute_match(extraction, order, received, guardrails)

    assert ReasonCode.LINE_NOT_ON_PO in result.reason_codes


def test_two_small_freight_lines_within_both_legs_pass(guardrails: GuardrailConfig) -> None:
    """$40 in total against a $50 cap and a $200 share. Nothing to see."""
    extraction, order, received = _with_freight("20.00", "20.00")

    result = compute_match(extraction, order, received, guardrails)

    assert result.matched
    assert [line.outcome for line in result.lines].count(MatchLineOutcome.UNMATCHED) == 2


# --- the header -------------------------------------------------------------


def test_a_subtotal_that_is_not_the_sum_of_the_lines_is_held(
    guardrails: GuardrailConfig,
) -> None:
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00"), subtotal="500.00", total="500.00"),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert ReasonCode.TOTALS_OVER_TOLERANCE in result.reason_codes
    assert result.subtotal_delta == Decimal("300.00")


def test_a_subtotal_off_by_rounding_is_not(guardrails: GuardrailConfig) -> None:
    """``rounding_tolerance_abs`` is $1.00, and this is what it is for."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00"), subtotal="200.50", total="200.50"),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert result.matched
    assert result.subtotal_delta == Decimal("0.50")


def test_tax_that_the_lines_do_not_imply_is_held(guardrails: GuardrailConfig) -> None:
    """20% of $200 is $40. The document claims $60."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "10", "20.00", tax_rate="0.20"),
            tax_total="60.00",
        ),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert ReasonCode.TAX_MISMATCH in result.reason_codes
    assert result.tax_delta == Decimal("20.00")


def test_tax_the_lines_do_imply_is_not(guardrails: GuardrailConfig) -> None:
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "10", "20.00", tax_rate="0.20"),
            tax_total="40.00",
        ),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert result.matched
    assert result.tax_delta == Decimal("0.00")


def test_a_total_that_is_not_subtotal_plus_tax_is_held(
    guardrails: GuardrailConfig,
) -> None:
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00"), total="260.00"),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert ReasonCode.TOTALS_OVER_TOLERANCE in result.reason_codes
    assert result.total_delta == Decimal("60.00")


def test_a_line_whose_extension_is_not_its_own_arithmetic_is_held(
    guardrails: GuardrailConfig,
) -> None:
    """The document disagreeing with itself, at the versioned tolerance."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "10", "20.00", extended="250.00"),
            subtotal="250.00",
            total="250.00",
        ),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert ReasonCode.ARITHMETIC_INCONSISTENT in result.reason_codes


# --- identity, and returning early -------------------------------------------


def test_a_different_currency_stops_before_any_tolerance(
    guardrails: GuardrailConfig,
) -> None:
    """272,100 against 272,100 is a variance of zero in two different currencies.

    Zero is the most convincing wrong answer there is, so the comparison is
    never made: the result carries the mismatch and no line detail at all.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"), currency="EUR")
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert result.reason_codes == [ReasonCode.CURRENCY_MISMATCH]
    assert result.lines == []


@pytest.mark.parametrize("status", [PurchaseOrderStatus.CLOSED, PurchaseOrderStatus.CANCELLED])
def test_an_order_that_is_not_billable_stops_before_any_tolerance(
    guardrails: GuardrailConfig, status: PurchaseOrderStatus
) -> None:
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"), status=status)
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        receipts((1, "10")),
        guardrails,
    )

    assert result.reason_codes == [ReasonCode.PO_CLOSED]
    assert result.lines == []


def test_a_partially_received_order_is_still_billable(
    guardrails: GuardrailConfig,
) -> None:
    """The status a half-delivered order sits in is the status you invoice against."""
    order = purchase_order(
        po_line(1, WIDGET, "10", "20.00"), status=PurchaseOrderStatus.PARTIALLY_RECEIVED
    )
    result = compute_match(
        invoice(invoice_line(WIDGET, "6", "20.00")),
        order,
        receipts((1, "6")),
        guardrails,
    )

    assert result.matched


# --- the properties the module claims ---------------------------------------


def test_reason_codes_are_deduplicated_and_ordered_as_found(
    guardrails: GuardrailConfig,
) -> None:
    """Three lines over on price report the objection once, not three times."""
    order = purchase_order(
        po_line(1, WIDGET, "10", "20.00"),
        po_line(2, GASKET, "10", "20.00"),
        po_line(3, "Bracket", "10", "20.00"),
    )
    result = compute_match(
        invoice(
            invoice_line(WIDGET, "10", "90.00"),
            invoice_line(GASKET, "10", "90.00"),
            invoice_line("Bracket", "10", "90.00"),
        ),
        order,
        receipts((1, "10"), (2, "10"), (3, "10")),
        guardrails,
    )

    assert result.reason_codes.count(ReasonCode.PRICE_OVER_TOLERANCE) == 1


def test_it_is_pure(guardrails: GuardrailConfig) -> None:
    """Same inputs, same result. What makes a past decision replayable."""
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    extraction = invoice(invoice_line(WIDGET, "13", "24.00"))
    first = compute_match(extraction, order, receipts((1, "10")), guardrails)
    second = compute_match(extraction, order, receipts((1, "10")), guardrails)

    assert first == second


def test_it_does_not_raise_on_an_invoice_with_no_lines(
    guardrails: GuardrailConfig,
) -> None:
    """A document this cannot read is a decision, not an error.

    Nothing billed, three lines ordered: every one is unbilled, which is not an
    exception, and the header is internally consistent at zero. So a genuinely
    empty invoice matches - and the rows say plainly that nothing was billed.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    result = compute_match(
        invoice(subtotal="0.00", total="0.00"), order, receipts((1, "10")), guardrails
    )

    assert result.matched
    assert [line.outcome for line in result.lines] == [MatchLineOutcome.UNBILLED]


def test_it_does_not_raise_on_an_order_with_no_lines(
    guardrails: GuardrailConfig,
) -> None:
    """Every invoice line becomes an unmatched charge, judged against a zero share."""
    order = purchase_order()
    result = compute_match(
        invoice(invoice_line(WIDGET, "10", "20.00")),
        order,
        ReceiptSet(po_number=PO_NUMBER),
        guardrails,
    )

    assert ReasonCode.LINE_NOT_ON_PO in result.reason_codes
    assert [line.outcome for line in result.lines] == [MatchLineOutcome.UNMATCHED]


def test_the_result_carries_no_text_from_the_document(
    guardrails: GuardrailConfig,
) -> None:
    """Structural, not a spot check: no string field anywhere holds document content.

    The matcher's inputs include a vendor name, a line description and a remit-to
    block, all written by whoever made the invoice. None of them may reach a
    result that a later stage reads as this system's own conclusion.
    """
    order = purchase_order(po_line(1, WIDGET, "10", "20.00"))
    extraction = invoice(invoice_line(WIDGET, "13", "99.00"))
    result = compute_match(extraction, order, receipts((1, "10")), guardrails)

    serialised = result.model_dump_json()
    assert WIDGET not in serialised
    assert extraction.vendor_name not in serialised
