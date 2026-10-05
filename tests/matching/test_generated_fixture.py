"""The matcher against every invoice the generator produced.

Sixty documents across ten orders, each with a ``truth.json`` written *before*
this code existed and never by evaluating a tolerance. That is what makes them
worth running: the generator declares what each planted defect should cause, and
this asserts that the matcher agrees. A fixture that graded itself would only
prove that one copy of a rule equals another.

**What the truth file is, exactly.** ``expected_reason_codes`` is the signature
of the planted defect, composed with ``receipt_missing`` on the one order that
received nothing - not an exhaustive list of everything a matcher will find. So
the assertion is two-sided rather than an equality: every declared matcher code
must appear, and an invoice declared ``MATCHED`` must produce *no* codes at all.
The one place where the matcher legitimately says more than the truth file does
is pinned by name in :func:`test_where_the_matcher_says_more_than_the_truth_file`,
so the gap is written down instead of hidden behind a subset check.

**Skipped when ``data/`` is absent, which is most checkouts.** Nothing under
``data/`` is committed - rule 6 - so these files exist only after ``just
generate`` and ``just seed``. The skip is loud rather than silent, and the
matcher's own behaviour is covered by ``test_compute_match.py``, which builds
everything it needs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.matching.conftest import GUARDRAILS_PATH, REPO_ROOT

from ap_agent.contracts.enums import ReasonCode
from ap_agent.contracts.generated import GeneratedVariant
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    ReceiptSet,
)
from ap_agent.matching import compute_match
from ap_agent.tools.get_purchase_order import to_purchase_order
from ap_agent.tools.get_receipts import to_receipt_set

if TYPE_CHECKING:
    from ap_agent.contracts.guardrails import GuardrailConfig
    from ap_agent.contracts.matching import MatchResult

GENERATED = REPO_ROOT / "data" / "generated"
INVOICES = GENERATED / "invoices"
CAPTURED_PO = REPO_ROOT / "tests" / "tools" / "qbo_purchase_order_response.json"

TRUTH_FILES = sorted(INVOICES.glob("*/*/truth.json"))

pytestmark = pytest.mark.skipif(
    not TRUTH_FILES or not (GENERATED / "receipts.json").is_file(),
    reason=(
        f"no generated fixture under {INVOICES}. Nothing under data/ is committed; "
        f"run `just seed` then `just generate` to produce it."
    ),
)

MATCHER_CODES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.PO_NOT_FOUND,
        ReasonCode.PO_CLOSED,
        ReasonCode.LINE_NOT_ON_PO,
        ReasonCode.PRICE_OVER_TOLERANCE,
        ReasonCode.QUANTITY_OVER_TOLERANCE,
        ReasonCode.RECEIPT_MISSING,
        ReasonCode.RECEIPT_PARTIAL,
        ReasonCode.TOTALS_OVER_TOLERANCE,
        ReasonCode.TAX_MISMATCH,
        ReasonCode.CURRENCY_MISMATCH,
        ReasonCode.ARITHMETIC_INCONSISTENT,
    }
)
"""The codes ``compute_match`` is allowed to raise.

The truth files carry codes from the whole vocabulary, because they describe
what should happen to a document rather than what one function decides.
``suspicious_document_content`` is the clearest case: the ``hidden_text``
variant plants white 4pt text instructing a reader to change the vendor's bank
details, its numbers match perfectly, and the code that holds it comes from the
output filter. The matcher never sees the text at all - it is not given a single
free-text field - so filtering the comparison to this set is not the test being
lenient, it is the test naming which stage owns which code.
"""


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def _truth_id(path: Path) -> str:
    return f"{path.parent.parent.name}/{path.parent.name}"


# ---------------------------------------------------------------------------
# the three sides of the match, as the system of record holds them
# ---------------------------------------------------------------------------


def _captured_order() -> PurchaseOrder:
    """AP-SEED-001 as QuickBooks actually returned it, through the real mapper.

    The one order in the fixture whose response was captured from the sandbox on
    2026-09-12. It goes through ``to_purchase_order`` rather than being hand
    built, so this test exercises the mapping the loop will use - item refs,
    ``LineNum``, float amounts and all.
    """
    response = _load(CAPTURED_PO)
    envelope = cast("dict[str, Any]", response["QueryResponse"])
    records = cast("list[dict[str, Any]]", envelope["PurchaseOrder"])
    return to_purchase_order(records[0])


def _order_from_manifest(record: dict[str, Any]) -> PurchaseOrder:
    """One order rebuilt from the seed manifest, for the nine not captured.

    **Only AP-SEED-001 has a captured QuickBooks response**, so the other nine
    are reconstructed from ``seed_manifest.json`` - the file the seeder wrote as
    it created them - and they are second-hand evidence by comparison: a mapping
    bug in ``to_purchase_order`` would not show up on any of them.

    Two things the manifest does not record are supplied here, and both are
    assumptions this test is making:

    * ``line_no`` is the 1-based position, which is how QuickBooks numbers
      purchase-order lines in creation order and how ``receipts.json`` was
      migrated to join.
    * ``item_ref`` is left ``None``, so these nine orders pair on description
      alone. That is the weaker of the two keys and the one the generated
      invoices exercise anyway - every ``po_line_ref`` in the fixture is null,
      because the renderer prints no purchase-order line references.
    """
    lines = cast("list[dict[str, Any]]", record["lines"])
    return PurchaseOrder(
        po_number=str(record["doc_number"]),
        erp_id=str(record["qbo_id"]),
        vendor_erp_id=str(record["vendor_qbo_id"]),
        vendor_name=str(record["vendor"]),
        currency=str(record["currency"]),
        po_date=record["order_date"],
        status=PurchaseOrderStatus.OPEN,
        lines=[
            PurchaseOrderLine(
                line_no=index,
                item_ref=None,
                description=str(line["item"]),
                qty_ordered=Decimal(str(line["qty"])),
                unit_price=Decimal(str(line["unit_price"])),
                extended=Decimal(str(line["amount"])),
            )
            for index, line in enumerate(lines, start=1)
        ],
    )


def _orders() -> dict[str, PurchaseOrder]:
    """Every seeded order: the captured one, and nine from the manifest."""
    manifest = _load(GENERATED / "seed_manifest.json")
    records = cast("list[dict[str, Any]]", manifest["purchase_orders"])
    built = {str(r["doc_number"]): _order_from_manifest(r) for r in records}
    captured = _captured_order()
    built[captured.po_number] = captured
    return built


def _receipt_sets() -> dict[str, ReceiptSet]:
    """Every receipt set, flattened by the same function the loop uses."""
    documents = cast("list[dict[str, Any]]", _load(GENERATED / "receipts.json")["receipts"])
    numbers = {str(document["po_number"]) for document in documents}
    return {
        po_number: to_receipt_set(
            po_number, [d for d in documents if str(d["po_number"]) == po_number]
        )
        for po_number in numbers
    }


def _extraction(truth: dict[str, Any]) -> InvoiceExtraction:
    """Build the extraction the reading model is expected to produce.

    From ``expected`` verbatim: this test is about the matcher, so it is handed
    a perfect reading. What a real model does with the page is the extraction
    seat's problem and has its own tests.
    """
    return InvoiceExtraction.model_validate(truth["expected"])


ORDERS = _orders() if TRUTH_FILES else {}
RECEIPTS = _receipt_sets() if TRUTH_FILES else {}


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


def _match(truth: dict[str, Any], guardrails: GuardrailConfig) -> MatchResult:
    po_number = str(truth["po_number"])
    return compute_match(
        _extraction(truth),
        ORDERS[po_number],
        RECEIPTS.get(po_number, ReceiptSet(po_number=po_number)),
        guardrails,
    )


@pytest.mark.parametrize("truth_path", TRUTH_FILES, ids=_truth_id)
def test_every_generated_invoice_matches_its_declared_truth(
    truth_path: Path, guardrails: GuardrailConfig
) -> None:
    """The whole fixture, one assertion per document.

    Declared ``MATCHED`` means the matcher must find nothing - an exact claim,
    and the one that would break first if a tolerance were widened by accident.
    Declared ``EXCEPTION`` means every matcher-owned code the generator planted
    must be present; it may find more, which is the case pinned separately
    below.
    """
    truth = _load(truth_path)
    result = _match(truth, guardrails)

    declared = {ReasonCode(code) for code in truth["expected_reason_codes"]} & MATCHER_CODES
    found = set(result.reason_codes)

    if truth["expected_match"] == "MATCHED":
        assert result.matched, f"expected a clean match, got {sorted(found)}"
        assert found == set()
    else:
        assert not result.matched
        assert declared <= found, f"planted {sorted(declared)}, matcher found {sorted(found)}"


@pytest.mark.parametrize("truth_path", TRUTH_FILES, ids=_truth_id)
def test_every_result_is_stamped_and_re_derivable(
    truth_path: Path, guardrails: GuardrailConfig
) -> None:
    """Every result names its ruleset, its order, and both sides of every line.

    The audit question is "why was this held", and it is answerable months later
    only if the row carries the numbers and the result names the file whose
    tolerances read them.
    """
    truth = _load(truth_path)
    result = _match(truth, guardrails)

    assert result.config_version == guardrails.config_version
    assert result.po_number == truth["po_number"]
    assert result.matched is (result.reason_codes == [])

    for line in result.lines:
        assert line.line_no is not None or line.invoice_line_index is not None
        if line.line_no is not None and line.invoice_line_index is not None:
            assert line.po_unit_price is not None
            assert line.invoice_unit_price is not None
            assert line.invoice_qty is not None


@pytest.mark.parametrize("truth_path", TRUTH_FILES, ids=_truth_id)
def test_no_document_text_reaches_the_result(truth_path: Path, guardrails: GuardrailConfig) -> None:
    """Not one line description from sixty documents appears on any result."""
    truth = _load(truth_path)
    result = _match(truth, guardrails)

    serialised = result.model_dump_json()
    for line in cast("list[dict[str, Any]]", truth["expected"]["line_items"]):
        assert str(line["description"]) not in serialised
    assert str(truth["vendor_name"]) not in serialised


# ---------------------------------------------------------------------------
# the cases the fixture exists to catch, named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("po_number", "billed", "ordered", "received"),
    [
        ("AP-SEED-010", 2, 11, 0),
        ("AP-SEED-009", 9, 9, 7),
    ],
)
def test_billing_inside_the_order_and_outside_the_receipt_is_caught(
    guardrails: GuardrailConfig, po_number: str, billed: int, ordered: int, received: int
) -> None:
    """The two invoices that separate a real three-way match from a two-way one.

    On AP-SEED-010 the vendor bills 2 of a line that ordered 11 and received
    nothing. On AP-SEED-009 they bill 9 of a line that ordered 9 and received 7.
    Both are comfortably inside what was authorised, and a matcher reading
    ``qty_ordered`` reports a clean match on both. Against the goods receipt
    both are bills for units nobody has.
    """
    assert billed <= ordered, "the point: this is inside the order"
    assert billed > received, "and outside the receipt"

    truth = _load(INVOICES / po_number / "qty_over_received" / "truth.json")
    result = _match(truth, guardrails)

    assert ReasonCode.QUANTITY_OVER_TOLERANCE in result.reason_codes


def test_where_the_matcher_says_more_than_the_truth_file(
    guardrails: GuardrailConfig,
) -> None:
    """AP-SEED-010's clean invoice, which is clean about a delivery that never came.

    The truth file declares ``receipt_missing`` alone, because that is what the
    *order* contributes and the ``clean`` variant plants no defect of its own.
    The matcher also reports ``quantity_over_tolerance``, and it is right to:
    the invoice bills 9, 11 and 10 units of three lines that have received
    nothing at all.

    Asserted exactly, so the one place these two disagree is a decision on the
    record rather than something the subset check upstairs quietly absorbs.
    """
    truth = _load(INVOICES / "AP-SEED-010" / "clean" / "truth.json")
    assert truth["expected_reason_codes"] == ["receipt_missing"]

    result = _match(truth, guardrails)

    assert set(result.reason_codes) == {
        ReasonCode.RECEIPT_MISSING,
        ReasonCode.QUANTITY_OVER_TOLERANCE,
    }


def test_the_partial_delivery_nobody_billed_is_not_an_exception(
    guardrails: GuardrailConfig,
) -> None:
    """AP-SEED-009: three lines ordered, one received nothing, and it is left off.

    The vendor invoiced 5 of the 5 that came and 7 of the 7 that came, and said
    nothing about the line that never arrived. That is a correct invoice and the
    matcher must pay it - while still showing the reviewer the outstanding line.
    """
    truth = _load(INVOICES / "AP-SEED-009" / "clean" / "truth.json")
    result = _match(truth, guardrails)

    assert result.matched
    unbilled = [line for line in result.lines if line.line_no == 2]
    assert len(unbilled) == 1
    assert unbilled[0].received_qty == Decimal("0.000000")


def test_the_two_freight_lines_land_on_either_side_of_the_same_limit(
    guardrails: GuardrailConfig,
) -> None:
    """$25 rides along and $120 does not, on every order in the fixture.

    Both fail or pass on the *absolute* leg: 2% of even the smallest seeded
    order is $862, so the percentage leg would wave $120 through everywhere.
    That is the AND in ``guardrails.v1.yaml`` earning its divergence from the
    architecture report, measured across ten orders rather than argued about.
    """
    for po_number in sorted(ORDERS):
        small = _match(_load(INVOICES / po_number / "freight_small" / "truth.json"), guardrails)
        large = _match(_load(INVOICES / po_number / "freight_large" / "truth.json"), guardrails)

        assert ReasonCode.LINE_NOT_ON_PO not in small.reason_codes, po_number
        assert ReasonCode.LINE_NOT_ON_PO in large.reason_codes, po_number


def test_the_fixture_is_the_size_it_should_be() -> None:
    """Ten orders, every variant. A sweep over four files would pass silently."""
    assert len(TRUTH_FILES) == 10 * len(GeneratedVariant)
    assert len(ORDERS) == 10


def test_the_guardrails_under_test_are_the_shipped_ones() -> None:
    """Not a double. The numbers are as much under test as the code."""
    assert GUARDRAILS_PATH.is_file()
