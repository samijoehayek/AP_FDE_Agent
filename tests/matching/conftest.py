"""Building blocks for the matcher's tests: an order, receipts, an invoice.

Small helpers rather than fixtures, because almost every test here varies one
number and a fixture that took eight override parameters would be harder to read
than the constructor it wraps.

The real ``config/guardrails.v1.yaml`` is loaded rather than a test double. The
numbers in that file are the thing under test as much as the code is - a matcher
proved correct against invented tolerances would say nothing about what this
system will actually hold or pay.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ap_agent.contracts.invoice import InvoiceExtraction, LineItem
from ap_agent.contracts.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    ReceiptLine,
    ReceiptSet,
)
from ap_agent.guardrails.config import load_guardrails

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDRAILS_PATH = REPO_ROOT / "config" / "guardrails.v1.yaml"

PO_NUMBER = "PO-TEST-1"
RECEIVED_ON = date(2026, 8, 1)


@pytest.fixture(scope="session")
def guardrails():  # noqa: ANN201 - the return type is GuardrailConfig, inferred
    """The shipped guardrails, not a double. See this module's docstring."""
    return load_guardrails(GUARDRAILS_PATH)


def po_line(
    line_no: int,
    description: str,
    qty: str,
    price: str,
    item_ref: str | None = None,
) -> PurchaseOrderLine:
    """One ordered line. ``extended`` is derived so it cannot disagree."""
    quantity = Decimal(qty)
    unit_price = Decimal(price)
    return PurchaseOrderLine(
        line_no=line_no,
        item_ref=item_ref,
        description=description,
        qty_ordered=quantity,
        unit_price=unit_price,
        extended=(quantity * unit_price).quantize(Decimal("0.01")),
    )


def purchase_order(
    *lines: PurchaseOrderLine,
    status: PurchaseOrderStatus = PurchaseOrderStatus.OPEN,
    currency: str = "USD",
) -> PurchaseOrder:
    """An order for one vendor, open unless a test says otherwise."""
    return PurchaseOrder(
        po_number=PO_NUMBER,
        erp_id="145",
        vendor_erp_id="58",
        vendor_name="Acme Industrial Supply Ltd",
        currency=currency,
        po_date=date(2026, 7, 27),
        status=status,
        lines=list(lines),
    )


def receipts(*quantities: tuple[int, str]) -> ReceiptSet:
    """What arrived, as ``(line_no, qty)`` pairs. Zero is a real answer."""
    return ReceiptSet(
        po_number=PO_NUMBER,
        lines=[
            ReceiptLine(line_no=line_no, qty_received=Decimal(qty), received_on=RECEIVED_ON)
            for line_no, qty in quantities
        ],
    )


def invoice_line(
    description: str,
    qty: str,
    price: str,
    *,
    extended: str | None = None,
    tax_rate: str = "0",
) -> LineItem:
    """One billed line. ``extended`` defaults to qty times price.

    No ``po_line_ref``: the generated invoices print none, so pairing here runs
    on descriptions exactly as it does against the fixture. The reference path
    has its own tests in ``test_pairing.py``.
    """
    quantity = Decimal(qty)
    unit_price = Decimal(price)
    computed = (quantity * unit_price).quantize(Decimal("0.01"))
    return LineItem(
        description=description,
        quantity=quantity,
        unit="EA",
        unit_price=unit_price,
        extended_price=Decimal(extended) if extended is not None else computed,
        tax_rate=Decimal(tax_rate),
    )


def invoice(
    *lines: LineItem,
    currency: str = "USD",
    subtotal: str | None = None,
    tax_total: str = "0.00",
    total: str | None = None,
) -> InvoiceExtraction:
    """An invoice whose header agrees with its lines unless a test breaks it."""
    lines_total = sum((line.extended_price for line in lines), Decimal(0)).quantize(Decimal("0.01"))
    resolved_subtotal = Decimal(subtotal) if subtotal is not None else lines_total
    tax = Decimal(tax_total)
    return InvoiceExtraction(
        vendor_name="Acme Industrial Supply Ltd",
        invoice_number="INV-2026-00187",
        invoice_date=date(2026, 8, 3),
        currency=currency,
        subtotal=resolved_subtotal,
        tax_total=tax,
        total=Decimal(total) if total is not None else resolved_subtotal + tax,
        po_references=[PO_NUMBER],
        line_items=list(lines),
    )
