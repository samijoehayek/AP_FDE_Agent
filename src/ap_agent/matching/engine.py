"""The three-way match: what was ordered, what arrived, what was billed.

This is the function that decides whether money is owed, and every property it
has is a property it has on purpose.

**It compares the invoice to what was *received*, never to what was ordered.**
That single choice is most of the value of a three-way match. An order for 11
monitors that has delivered none is an order a vendor can bill 2 against and
still look correct against the paperwork - the quantity is well inside what was
authorised. Against the goods receipt it is a bill for two monitors nobody has.
``data/generated/invoices/AP-SEED-010/qty_over_received`` is exactly that
invoice, and it exists to fail this module if anyone ever reaches for
``qty_ordered``.

**Every two-legged tolerance requires both legs.** A percentage band and an
absolute cap must *both* be satisfied for a line to pass, so the tighter one
binds. Either one alone is a licence somewhere: 2% of a $272,000 order is $5,400
of unexplained freight, and $50 on a $12 line is four times the line. The
guardrails file writes the unmatched-charge rule as an AND for this reason, and
diverges from the architecture report, which writes it as an OR.

**It never raises.** An invoice this cannot make sense of is a ``MatchResult``
with reasons on it, not an exception. A raise would travel up as an *error* -
something broken, to be retried - when the correct handling is a routing
decision that puts the document in front of a person. The loop has one path for
"this needs a human" and it is fed by reason codes.

**No free text enters and none leaves.** Line descriptions are used as join keys
in :mod:`ap_agent.matching.pairing` and nowhere else. ``suspicious_text``,
``remit_to_display``, ``payment_terms`` and ``vendor_name`` are never read here:
a document's prose has no business influencing an arithmetic decision, and the
cheapest way to guarantee that is a module that does not mention the fields.

**Pure.** No clock, no filesystem, no model, no ERP. Same inputs, same result,
forever - which is what makes a decision from last quarter replayable under last
quarter's config, and what lets the whole thing be tested against the generated
fixture without a network.

What it deliberately does not do: identity. Whether this purchase order belongs
to this vendor is settled before the matcher is called, because it is a
comparison of ERP ids and the matcher is given no vendor. See
``_match_prep_step`` in the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from ap_agent.contracts.enums import MatchLineOutcome, ReasonCode
from ap_agent.contracts.matching import MatchLine, MatchResult
from ap_agent.contracts.purchase_order import PurchaseOrderStatus
from ap_agent.matching.pairing import pair_lines

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from ap_agent.contracts.guardrails import GuardrailConfig, Tolerances
    from ap_agent.contracts.invoice import InvoiceExtraction, LineItem
    from ap_agent.contracts.purchase_order import PurchaseOrder, PurchaseOrderLine, ReceiptSet
    from ap_agent.matching.pairing import IndexedInvoiceLine, LinePair

__all__ = ["BILLABLE_STATUSES", "compute_match"]

_MONEY = Decimal("0.01")
_QUANTITY = Decimal("0.000001")
_PERCENT = Decimal("0.0001")
_HUNDRED = Decimal(100)

BILLABLE_STATUSES: frozenset[PurchaseOrderStatus] = frozenset(
    {PurchaseOrderStatus.OPEN, PurchaseOrderStatus.PARTIALLY_RECEIVED}
)
"""Statuses against which an invoice may still be matched.

``PARTIALLY_RECEIVED`` is in here, and it is the one worth defending: an order
half delivered is precisely the order a vendor invoices against, and treating it
as unbillable would hold every partial delivery in the system. QuickBooks emits
only ``Open`` and ``Closed``, so today this is a one-member set in practice; the
other ERPs the contract anticipates will use it.

``CANCELLED`` and ``CLOSED`` both raise :attr:`ReasonCode.PO_CLOSED`. The
vocabulary has no separate code for a cancelled order and inventing one here
would put a reason on a ``MatchResult`` that
:class:`~ap_agent.contracts.exceptions.ExceptionClassification` could not
describe - the two share one vocabulary so that a rule and an explanation can
never disagree about what went wrong.
"""


def _money(value: Decimal) -> Decimal:
    """Round a derived amount to the minor unit, for storage only.

    The ``Money`` annotation refuses to round, because an amount transcribed
    from a document must survive unchanged or fail loudly. A delta computed here
    is not a transcription - it is this module's own arithmetic, and it has to
    land on something a person can read.

    So the rounding is deliberate and it happens *after* every comparison. Each
    tolerance test below runs on the exact value; only the number written to the
    result passes through here. A tolerance evaluated on a rounded figure would
    make a half-cent the difference between paying and not.
    """
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _quantity(value: Decimal) -> Decimal:
    """Round a quantity to the contract's six places. See :func:`_money`."""
    return value.quantize(_QUANTITY, rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> Decimal:
    """Round a percentage to the contract's four places. See :func:`_money`."""
    return value.quantize(_PERCENT, rounding=ROUND_HALF_UP)


class _Reasons:
    """The reason codes found so far, deduplicated, in the order they were found.

    Order carries meaning on the result - the first code is the first thing the
    matcher objected to, which is the one a reviewer should read first - and a
    plain ``set`` would throw that away while a plain ``list`` would report the
    same objection once per line.
    """

    def __init__(self) -> None:
        self._codes: list[ReasonCode] = []

    def add(self, code: ReasonCode) -> None:
        if code not in self._codes:
            self._codes.append(code)

    def as_list(self) -> list[ReasonCode]:
        return list(self._codes)


def compute_match(
    extraction: InvoiceExtraction,
    po: PurchaseOrder,
    receipts: ReceiptSet,
    config: GuardrailConfig,
) -> MatchResult:
    """Match one invoice against one purchase order and its receipts.

    Args:
        extraction: The reading of the document. Only its numbers, currency and
            line items are consulted; no prose field is read.
        po: The order, as the ERP holds it. The authority for price.
        receipts: What arrived against that order. The authority for quantity.
            A purchase order with nothing received is an empty set, never
            ``None`` - see :class:`~ap_agent.contracts.purchase_order.ReceiptSet`.
        config: The versioned guardrails. Its ``config_version`` is stamped on
            the result so the decision stays re-explainable under the rules that
            actually made it.

    Returns:
        A :class:`~ap_agent.contracts.matching.MatchResult`. ``matched`` is true
        if and only if no reason code was raised.

    The order of the checks is itself a decision:

    1. **Identity** - currency, then status. Both return immediately, because a
       tolerance applied to the wrong order or the wrong currency produces a
       number that looks like an answer. Comparing 272,100 INR to 272,100 USD
       gives a variance of zero, and zero is the most convincing wrong answer
       available.
    2. **Pairing** - which invoice line bills which ordered line.
    3. **Per line** - quantity against what was received, then unit price
       against what was ordered, then the line's own extension.
    4. **Unmatched charges** - what the invoice bills that no ordered line
       covers. Freight is the ordinary case, and it is judged, not rejected.
    5. **Header** - the three totals, which by this point should only differ by
       rounding, because anything larger has already been caught on a line.

    Never raises. Every failure this can have is a reason code.
    """
    reasons = _Reasons()
    tolerances = config.tolerances

    if extraction.currency != po.currency:
        # No lines on the result. A line-by-line breakdown of two different
        # currencies would be arithmetic nobody should be invited to read.
        reasons.add(ReasonCode.CURRENCY_MISMATCH)
        return _result(reasons, config, po, [], extraction)

    if po.status not in BILLABLE_STATUSES:
        reasons.add(ReasonCode.PO_CLOSED)
        return _result(reasons, config, po, [], extraction)

    paired = pair_lines(extraction.line_items, po.lines)

    lines: list[MatchLine] = []
    lines.extend(_check_pairs(paired.pairs, receipts, tolerances, reasons))
    lines.extend(_check_unmatched(paired.unmatched, po.lines, tolerances, reasons))
    lines.extend(_unbilled_rows(paired.unbilled, receipts))

    _check_header(extraction, tolerances, reasons)
    return _result(reasons, config, po, lines, extraction)


# ---------------------------------------------------------------------------
# paired lines
# ---------------------------------------------------------------------------


def _check_pairs(
    pairs: Sequence[LinePair],
    receipts: ReceiptSet,
    tolerances: Tolerances,
    reasons: _Reasons,
) -> list[MatchLine]:
    """Judge every invoice line that has an ordered line behind it.

    Quantity is decided per *purchase-order line* and price per *invoice line*,
    and the asymmetry is the point. Two invoice lines billing 2 each against a
    receipt of 3 are individually unremarkable and together an over-bill, so the
    quantities are summed before the comparison. Their prices are their own:
    averaging them would let a correct line pay for an inflated one.

    So a purchase-order line billed twice produces two rows carrying the same
    summed ``invoice_qty`` and the same quantity verdict, each with its own
    price. That repetition is deliberate. The result exists so a reviewer can
    re-derive the decision from the row in front of them, and a row showing only
    its own 2 against a receipt of 3 would make the exception unexplainable.
    """
    rows: list[MatchLine] = []
    for po_line in _ordered_unique(pair.po_line for pair in pairs):
        group = [pair for pair in pairs if pair.po_line.line_no == po_line.line_no]
        quantity = _judge_quantity(group, receipts.quantity_for(po_line.line_no), tolerances)

        if quantity.nothing_arrived:
            # Ordered, billed, and not a single unit of it has turned up. This
            # is the reason code the third leg of the match exists for, and it
            # is raised alongside the quantity variance rather than instead of
            # it: "nothing arrived" and "you billed for more than arrived" are
            # both true and a reviewer needs both.
            reasons.add(ReasonCode.RECEIPT_MISSING)
        if quantity.over:
            reasons.add(ReasonCode.QUANTITY_OVER_TOLERANCE)

        rows.extend(_pair_row(pair, po_line, quantity, tolerances, reasons) for pair in group)
    return rows


@dataclass(frozen=True)
class _Quantity:
    """The quantity verdict for one purchase-order line, and the numbers behind it.

    One object rather than three loose arguments because the three only ever
    travel together: every row in the group carries the same summed quantity,
    the same received quantity and the same verdict.
    """

    billed: Decimal
    received: Decimal | None
    over: bool

    @property
    def nothing_arrived(self) -> bool:
        """True when no unit of this line has turned up.

        ``None`` and zero are different facts and the row records which one it
        was, but they are the same answer to "did anything arrive": no.
        """
        return self.received is None or self.received == 0


def _judge_quantity(
    group: Sequence[LinePair], received: Decimal | None, tolerances: Tolerances
) -> _Quantity:
    """Sum what this purchase-order line was billed, and compare it to what came."""
    billed = _quantity(sum((pair.invoice.line.quantity for pair in group), Decimal(0)))
    return _Quantity(
        billed=billed, received=received, over=_is_over_quantity(billed, received, tolerances)
    )


def _is_over_quantity(billed: Decimal, received: Decimal | None, tolerances: Tolerances) -> bool:
    """Decide whether this line bills for more than arrived.

    **Against ``received``, never ``qty_ordered``.** The purchase order says what
    was authorised; only the goods receipt says what exists. Comparing to the
    order would pass an invoice for two units of something a warehouse has never
    seen, provided the order was large enough - which is the whole failure this
    function is here to prevent.

    ``qty_over_billing_pct`` is zero in ``guardrails.v1.yaml`` and is read from
    the config anyway. Over-billing is not a measurement error with a sensible
    band around it, but the number that says so belongs in the reviewed file
    rather than in this expression.

    No receipt line at all is treated as nothing received. The distinction
    ``ReceiptSet`` preserves between ``None`` and zero is real and it is
    recorded on the row; it is not a difference in what may be billed, because
    neither answer is evidence that anything arrived.
    """
    arrived = received if received is not None else Decimal(0)
    allowed = arrived * (_HUNDRED + tolerances.qty_over_billing_pct) / _HUNDRED
    return billed > allowed


def _pair_row(
    pair: LinePair,
    po_line: PurchaseOrderLine,
    quantity: _Quantity,
    tolerances: Tolerances,
    reasons: _Reasons,
) -> MatchLine:
    """Build one row, and decide its price and extension along the way."""
    invoice_line = pair.invoice.line
    variance_abs = invoice_line.unit_price - po_line.unit_price
    variance_pct = _variance_pct(variance_abs, po_line.unit_price)

    over_price = _is_over_price(variance_abs, variance_pct, tolerances)
    if over_price:
        reasons.add(ReasonCode.PRICE_OVER_TOLERANCE)

    if not _extension_holds(invoice_line, tolerances):
        # The document disagrees with itself: the line's own total is not its
        # quantity times its own price. The contract flags this too, at a fixed
        # tolerance it applies at construction; it is re-checked here against
        # the *versioned* one, because what counts as rounding is a policy
        # number and policy numbers live in the guardrails file.
        reasons.add(ReasonCode.ARITHMETIC_INCONSISTENT)

    return MatchLine(
        outcome=_pair_outcome(over_quantity=quantity.over, over_price=over_price),
        line_no=po_line.line_no,
        invoice_line_index=pair.invoice.index,
        invoice_qty=quantity.billed,
        received_qty=_quantity(quantity.received) if quantity.received is not None else None,
        po_unit_price=po_line.unit_price,
        invoice_unit_price=invoice_line.unit_price,
        price_variance_pct=variance_pct if variance_pct is None else _percent(variance_pct),
        price_variance_abs=_money(variance_abs),
    )


def _pair_outcome(*, over_quantity: bool, over_price: bool) -> MatchLineOutcome:
    """One outcome per row, quantity first when a line fails both tests.

    Quantity wins because it is the more serious of the two claims. A price
    variance is an argument about what a thing costs; a quantity over the
    receipt is a bill for goods that do not exist. Both numbers stay on the row,
    so nothing is lost by the ordering - it only decides which word the row
    leads with.
    """
    if over_quantity:
        return MatchLineOutcome.QTY_OVER
    if over_price:
        return MatchLineOutcome.PRICE_OVER
    return MatchLineOutcome.OK


def _variance_pct(variance_abs: Decimal, po_unit_price: Decimal) -> Decimal | None:
    """The price variance as a percentage of the ordered price, or None.

    ``None`` when the order priced the line at zero, because the percentage is
    undefined rather than infinite and reporting a huge number would suggest a
    measurement was taken. The absolute leg still decides the line, which is the
    conservative direction: any positive amount on a line ordered at zero is
    over by the whole amount.
    """
    if po_unit_price == 0:
        return None
    return variance_abs / po_unit_price * _HUNDRED


def _is_over_price(
    variance_abs: Decimal, variance_pct: Decimal | None, tolerances: Tolerances
) -> bool:
    """Decide whether the billed unit price is too far above the ordered one.

    **Both legs must hold to pass, so either one failing fails the line.** A
    percentage alone is a licence on a large line - 2% of a $21,400 printer is
    $428 - and an absolute cap alone is absurd on a small one, where $50 can be
    several times the price. Requiring both means the tighter leg always binds.

    Only the upward direction is tested. A vendor billing *below* the ordered
    price is the vendor's loss and nobody's exception, and holding an invoice
    for being too cheap would train reviewers to approve without reading.
    """
    if variance_abs <= 0:
        return False
    if variance_abs > tolerances.price_variance_abs:
        return True
    return variance_pct is not None and variance_pct > tolerances.price_variance_pct


def _extension_holds(invoice_line: LineItem, tolerances: Tolerances) -> bool:
    """Whether the line's printed total equals its quantity times its price."""
    computed = invoice_line.quantity * invoice_line.unit_price
    return abs(invoice_line.extended_price - computed) <= tolerances.rounding_tolerance_abs


def _ordered_unique(po_lines: Iterable[PurchaseOrderLine]) -> list[PurchaseOrderLine]:
    """Purchase-order lines in first-seen order, one entry each."""
    seen: set[int] = set()
    unique: list[PurchaseOrderLine] = []
    for po_line in po_lines:
        if po_line.line_no not in seen:
            seen.add(po_line.line_no)
            unique.append(po_line)
    return unique


# ---------------------------------------------------------------------------
# unmatched charges and unbilled lines
# ---------------------------------------------------------------------------


def _check_unmatched(
    unmatched: Sequence[IndexedInvoiceLine],
    po_lines: Sequence[PurchaseOrderLine],
    tolerances: Tolerances,
    reasons: _Reasons,
) -> list[MatchLine]:
    """Judge what the invoice bills that no ordered line covers.

    Freight, handling, a pallet deposit: real charges that no buyer puts on a
    purchase order, and rejecting them outright would send every delivery in the
    system to a person. So they are judged against a band rather than refused.

    **Both legs again, and here the AND diverges from the architecture report**,
    which writes this rule as "≤ $50 *or* ≤ 2% of PO value". The OR is the
    looser reading and it fails in the expensive direction: on the $651,550
    order in the seeded fixture the percentage leg alone waves through $13,031
    of unexplained charges. With the AND the absolute cap binds on every order
    in that fixture, and the percentage only ever tightens the rule on a small
    one.

    The share is taken against the sum of the ordered lines rather than a stored
    header total. A total the matcher computes from the lines it is comparing
    cannot disagree with those lines; a stored one can, and then the tolerance
    is a percentage of a number nobody checked.

    **Each line, and then all of them together.** A per-line test alone is
    defeated by arithmetic: $120 of freight printed as three $40 lines passes a
    $50 cap three times. So the unmatched charges are also summed and the sum is
    judged against the same two legs. The per-line test stays, because a credit
    line is negative and would let a $100 charge hide behind a $60 discount in
    the sum.
    """
    if not unmatched:
        return []

    po_total = sum((po_line.extended for po_line in po_lines), Decimal(0))
    share = po_total * tolerances.unmatched_charge_pct / _HUNDRED

    combined = sum((item.line.extended_price for item in unmatched), Decimal(0))
    if _is_over_unmatched(combined, share, tolerances):
        reasons.add(ReasonCode.LINE_NOT_ON_PO)

    rows: list[MatchLine] = []
    for item in unmatched:
        if _is_over_unmatched(item.line.extended_price, share, tolerances):
            reasons.add(ReasonCode.LINE_NOT_ON_PO)
        rows.append(
            MatchLine(
                outcome=MatchLineOutcome.UNMATCHED,
                invoice_line_index=item.index,
                invoice_qty=item.line.quantity,
                invoice_unit_price=item.line.unit_price,
            )
        )
    return rows


def _is_over_unmatched(amount: Decimal, share: Decimal, tolerances: Tolerances) -> bool:
    """Whether an unmatched amount breaks either leg. Both must hold to pass."""
    return amount > tolerances.unmatched_charge_abs or amount > share


def _unbilled_rows(unbilled: Sequence[PurchaseOrderLine], receipts: ReceiptSet) -> list[MatchLine]:
    """Record every ordered line nobody billed, raising no reason code.

    A vendor invoicing part of an order is the ordinary shape of a partial
    delivery, not a defect, and holding the invoice for it would mean nothing
    ever paid until an order completed. The rows are here so a reviewer looking
    at the result can see what is still outstanding without opening the ERP -
    and so that an invoice which quietly dropped a line is visible rather than
    merely absent.
    """
    return [
        MatchLine(
            outcome=MatchLineOutcome.UNBILLED,
            line_no=po_line.line_no,
            received_qty=_received_qty(receipts, po_line.line_no),
            po_unit_price=po_line.unit_price,
        )
        for po_line in unbilled
    ]


def _received_qty(receipts: ReceiptSet, line_no: int) -> Decimal | None:
    """Received quantity for one line, at the contract's scale, or None."""
    received = receipts.quantity_for(line_no)
    return _quantity(received) if received is not None else None


# ---------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------


def _check_header(extraction: InvoiceExtraction, tolerances: Tolerances, reasons: _Reasons) -> None:
    """Check the three totals, which should differ only by rounding by now.

    That is the whole point of doing this last. Every line has already been
    compared to the order and the receipt, so a header that still disagrees is
    telling you something the lines did not: a charge that never appeared as a
    line, a tax computed on a different base, a total that is not the sum of its
    own parts. If this fires, something is wrong with the document itself rather
    than with the deal.

    Tax gets its own absolute band and no percentage one. A percentage tolerance
    on tax is a percentage of a percentage, and it stops corresponding to any
    amount of money a person could reason about.
    """
    if abs(_subtotal_delta(extraction)) > tolerances.rounding_tolerance_abs:
        reasons.add(ReasonCode.TOTALS_OVER_TOLERANCE)

    if abs(_tax_delta(extraction)) > tolerances.tax_variance_abs:
        reasons.add(ReasonCode.TAX_MISMATCH)

    if abs(_total_delta(extraction)) > tolerances.rounding_tolerance_abs:
        reasons.add(ReasonCode.TOTALS_OVER_TOLERANCE)


def _subtotal_delta(extraction: InvoiceExtraction) -> Decimal:
    """Printed subtotal minus the sum of the printed lines. Signed."""
    lines = sum((line.extended_price for line in extraction.line_items), Decimal(0))
    return extraction.subtotal - lines


def _tax_delta(extraction: InvoiceExtraction) -> Decimal:
    """Printed tax minus the tax the lines' own rates imply. Signed.

    ``tax_rate`` is a fraction on the contract - 0.20 is twenty percent - and it
    is per line, because a single invoice routinely carries a standard-rated and
    a zero-rated line together.
    """
    implied = sum(
        (line.extended_price * line.tax_rate for line in extraction.line_items), Decimal(0)
    )
    return extraction.tax_total - implied


def _total_delta(extraction: InvoiceExtraction) -> Decimal:
    """Printed total minus subtotal plus tax. Signed. Rounding lives here."""
    return extraction.total - (extraction.subtotal + extraction.tax_total)


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


def _result(
    reasons: _Reasons,
    config: GuardrailConfig,
    po: PurchaseOrder,
    lines: list[MatchLine],
    extraction: InvoiceExtraction,
) -> MatchResult:
    """Assemble the result. ``matched`` is derived, never asserted separately."""
    codes = reasons.as_list()
    return MatchResult(
        matched=not codes,
        reason_codes=codes,
        config_version=config.config_version,
        po_number=po.po_number,
        lines=lines,
        subtotal_delta=_money(_subtotal_delta(extraction)),
        tax_delta=_money(_tax_delta(extraction)),
        total_delta=_money(_total_delta(extraction)),
    )
