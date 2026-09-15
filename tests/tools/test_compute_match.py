"""The tool wrapper around the matcher: unpacking, and the two refusals.

What the matcher decides is tested in ``tests/matching/``. This is about the
envelope - which order, which receipts, which config - and about the payloads
that have no match to attempt at all.
"""

from __future__ import annotations

from pathlib import Path
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

from ap_agent.contracts.enums import ReasonCode
from ap_agent.contracts.purchase_order import ReceiptSet
from ap_agent.guardrails.config import GuardrailsError
from ap_agent.tools.compute_match import ComputeMatchInput, compute_match

if TYPE_CHECKING:
    from ap_agent.contracts.invoice import InvoiceExtraction
    from ap_agent.contracts.purchase_order import PurchaseOrder

WIDGET = "Widget 10mm"
CONFIG_VERSION = "guardrails_v1"


def _extraction() -> InvoiceExtraction:
    return invoice(invoice_line(WIDGET, "10", "20.00"))


def _order() -> PurchaseOrder:
    return purchase_order(po_line(1, WIDGET, "10", "20.00"))


def _payload(**overrides: object) -> ComputeMatchInput:
    fields: dict[str, object] = {
        "invoice_id": "01M2KK1T5WND3KKMF67M6XM955",
        "extraction": _extraction(),
        "purchase_orders": [_order()],
        "receipts": [receipts((1, "10"))],
        "config_version": CONFIG_VERSION,
    }
    fields.update(overrides)
    return ComputeMatchInput.model_validate(fields)


def test_it_unpacks_the_envelope_and_matches() -> None:
    result = compute_match(_payload()).result

    assert result.matched
    assert result.po_number == PO_NUMBER
    assert result.config_version == CONFIG_VERSION


def test_it_picks_the_receipt_set_for_this_order() -> None:
    """Matched on ``po_number``, not on position in the list."""
    other = ReceiptSet(po_number="SOME-OTHER-PO")
    result = compute_match(_payload(receipts=[other, receipts((1, "10"))])).result

    assert result.matched


def test_an_absent_receipt_set_means_nothing_arrived() -> None:
    """Never a lookup to retry. "Nothing came" is the fact that holds an invoice."""
    result = compute_match(_payload(receipts=[])).result

    assert not result.matched
    assert ReasonCode.RECEIPT_MISSING in result.reason_codes


def test_no_purchase_order_is_a_reason_code_not_an_exception() -> None:
    """An order that is not there is an ordinary business outcome."""
    result = compute_match(_payload(purchase_orders=[], receipts=[])).result

    assert result.reason_codes == [ReasonCode.PO_NOT_FOUND]
    assert result.lines == []
    assert result.po_number == PO_NUMBER, "falls back to what the document printed"


def test_several_purchase_orders_are_refused_rather_than_guessed_at() -> None:
    """Matching against the first would be the worst available outcome.

    Every line belonging to the other orders would read as a charge nobody
    ordered, and the result would carry a confident verdict about an invoice it
    had only half looked at.
    """
    second = _order().model_copy(update={"po_number": "PO-TEST-2"})
    result = compute_match(_payload(purchase_orders=[_order(), second])).result

    assert result.reason_codes == [ReasonCode.MISSING_PO_REFERENCE]
    assert result.lines == []


def test_a_config_version_the_loader_does_not_hold_is_refused() -> None:
    """The one thing here that *does* raise, and deliberately.

    A run deciding under one ruleset while stamping another on every result
    would make the audit trail confidently wrong about why each invoice was
    held. That is worth a loud failure on the first invoice.
    """
    with pytest.raises(GuardrailsError, match="guardrails_v99"):
        compute_match(_payload(config_version="guardrails_v99"))


def test_the_config_comes_from_the_file_not_from_the_caller() -> None:
    """The stamped version is the loaded file's, so it cannot name a file nobody read."""
    result = compute_match(_payload()).result

    loaded = Path("config/guardrails.v1.yaml")
    assert loaded.is_file()
    assert result.config_version == CONFIG_VERSION
