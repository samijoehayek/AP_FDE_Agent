"""A generated invoice, its own truth file, and the loop - with no model at all.

This is the test the PO-backed fixture was built for. The generator renders an
invoice from a seeded purchase order; the truth file beside it says what is on
the page; and the loop is driven with that truth standing in for the extraction
model, the invoice's *own text layer* standing in for the second reading, and
the real confidence check between them.

What is real here: the renderer, the confidence check, ``lookup_vendor`` against
a real vendor master, ``get_receipts`` against a real receipts file, and the
whole state machine. What is faked: the two model seats, and QuickBooks.

Nothing is written under ``data/`` and nothing leaves the machine.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pymupdf
import pytest

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.generated import GeneratedInvoiceTruth, GeneratedVariant
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.contracts.vendor import VendorMatchBasis
from ap_agent.loop.runner import RunContext, run
from ap_agent.states.machine import InvoiceState
from ap_agent.tools.extract_invoice_text import ExtractInvoiceTextOutput
from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionOutput
from ap_agent.tools.get_purchase_order import GetPurchaseOrderOutput
from ap_agent.tools.get_receipts import GetReceiptsInput, GetReceiptsOutput, get_receipts
from ap_agent.tools.ingest_document import IngestDocumentOutput
from ap_agent.tools.lookup_vendor import LookupVendorInput, LookupVendorOutput, lookup_vendor
from ap_agent.tools.pymupdf_types import PdfDocument
from scripts import generate_invoices as gen

if TYPE_CHECKING:
    from ap_agent.tools.extract_invoice_text import ExtractInvoiceTextInput
    from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionInput
    from ap_agent.tools.get_purchase_order import GetPurchaseOrderInput

PO_NUMBER = "AP-TEST-001"
VENDOR = "Northwind Peripherals Inc"
VENDOR_ERP_ID = "62"
OTHER_VENDOR_ERP_ID = "67"

VENDOR_MASTER = f"""
version: "v1"
vendors:
  - display_name: "{VENDOR}"
    erp_id: "{VENDOR_ERP_ID}"
    currency: "USD"
    tax_id: "27-6653019"
    country: "US"
    address:
      - "6600 Cedar Bluff Parkway"
      - "Saint Paul, MN 55114"
  - display_name: "Vasanth Industrial Tools"
    erp_id: "{OTHER_VENDOR_ERP_ID}"
    currency: "INR"
    tax_id: "33AAHCV2298N1ZR"
    country: "IN"
    address:
      - "No. 7, Ambattur Industrial Estate"
      - "Chennai 600058"
"""

MANIFEST: dict[str, Any] = {
    "home_currency": "USD",
    "purchase_orders": [
        {
            "qbo_id": "145",
            "doc_number": PO_NUMBER,
            "vendor": VENDOR,
            "vendor_qbo_id": VENDOR_ERP_ID,
            "currency": "USD",
            "order_date": "2026-07-27",
            "total": "6400.00",
            "lines": [
                {"item": "Widget", "qty": "4", "unit_price": "1500.00", "amount": "6000.00"},
                {"item": "Gasket", "qty": "2", "unit_price": "200.00", "amount": "400.00"},
            ],
        }
    ],
}

RECEIPTS: dict[str, Any] = {
    "receipts": [
        {
            "receipt_id": f"GR-{PO_NUMBER}",
            "po_number": PO_NUMBER,
            "status": "full",
            "received_on": "2026-07-27",
            "lines": [
                {"item": "Widget", "qty_ordered": "4", "qty_received": "4"},
                {"item": "Gasket", "qty_ordered": "2", "qty_received": "2"},
            ],
        }
    ]
}


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    """The generated invoice, its truth file, and the files behind both."""
    (tmp_path / "seed_manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (tmp_path / "receipts.json").write_text(json.dumps(RECEIPTS), encoding="utf-8")
    (tmp_path / "vendor_master.yaml").write_text(VENDOR_MASTER, encoding="utf-8")

    gen.generate(
        tmp_path / "invoices",
        paths=gen.SeedPaths(
            manifest=tmp_path / "seed_manifest.json",
            receipts=tmp_path / "receipts.json",
            vendor_master=tmp_path / "vendor_master.yaml",
        ),
    )
    return tmp_path


def _truth(root: Path, variant: GeneratedVariant = GeneratedVariant.CLEAN) -> GeneratedInvoiceTruth:
    path = root / "invoices" / PO_NUMBER / variant.value / "truth.json"
    return GeneratedInvoiceTruth.model_validate_json(path.read_text(encoding="utf-8"))


def _pdf(root: Path, variant: GeneratedVariant = GeneratedVariant.CLEAN) -> Path:
    return root / "invoices" / PO_NUMBER / variant.value / "invoice.pdf"


def _page_text(pdf: Path) -> str:
    """The invoice's own text layer, which is what the second seat would read."""
    with cast("PdfDocument", pymupdf.open(pdf)) as document:
        return "\n".join(document[index].get_text("text") for index in range(document.page_count))


def _extraction_from(truth: GeneratedInvoiceTruth) -> InvoiceExtraction:
    """Build the extraction the model would have returned, from the truth file.

    No model call. The truth file states what is on the page, so a perfect
    reading of that page is exactly this - which makes every routing decision
    downstream a test of the routing rather than of the extraction.
    """
    return InvoiceExtraction(**truth.expected.model_dump())


def _purchase_order(vendor_erp_id: str = VENDOR_ERP_ID) -> PurchaseOrder:
    """What QuickBooks would return for this order, built from the same manifest."""
    order = MANIFEST["purchase_orders"][0]
    return PurchaseOrder(
        po_number=order["doc_number"],
        erp_id=order["qbo_id"],
        vendor_erp_id=vendor_erp_id,
        vendor_name=order["vendor"],
        currency=order["currency"],
        po_date=date.fromisoformat(order["order_date"]),
        status=PurchaseOrderStatus.OPEN,
        lines=[
            PurchaseOrderLine(
                line_no=index,
                item_ref=str(index),
                description=line["item"],
                qty_ordered=line["qty"],
                unit_price=line["unit_price"],
                extended=line["amount"],
            )
            for index, line in enumerate(order["lines"], start=1)
        ],
    )


def _context(
    root: Path,
    truth: GeneratedInvoiceTruth,
    *,
    po_vendor_erp_id: str = VENDOR_ERP_ID,
    variant: GeneratedVariant = GeneratedVariant.CLEAN,
) -> RunContext:
    """The loop with both model seats faked and everything else real."""
    extraction = _extraction_from(truth)
    page = _page_text(_pdf(root, variant))
    master = root / "vendor_master.yaml"
    receipts_file = root / "receipts.json"

    def _ingest(_payload: object) -> IngestDocumentOutput:
        return IngestDocumentOutput(
            sha256="b" * 64,
            byte_size=2048,
            media_type="application/pdf",
            page_count=1,
            has_text_layer=True,
            text_char_count=len(page),
            pages_with_text=1,
            page_sharpness=[900.0],
        )

    def _vision(_payload: ExtractInvoiceVisionInput) -> ExtractInvoiceVisionOutput:
        return ExtractInvoiceVisionOutput(
            extraction=extraction,
            model_id="claude-sonnet-5",
            prompt_version="extract_v1",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
        )

    def _text(_payload: ExtractInvoiceTextInput) -> ExtractInvoiceTextOutput:
        return ExtractInvoiceTextOutput(
            raw_text=page,
            pages=[page],
            has_text_layer=True,
            second_read=extraction,
            model_id="claude-haiku-4-5-20251001",
            prompt_version="extract_text_v1",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
        )

    def _lookup(payload: LookupVendorInput) -> LookupVendorOutput:
        # The real tool, pointed at this fixture's master.
        return lookup_vendor(payload.model_copy(update={"master_path": master}))

    def _get_po(_payload: GetPurchaseOrderInput) -> GetPurchaseOrderOutput:
        return GetPurchaseOrderOutput(
            purchase_order=_purchase_order(po_vendor_erp_id), latency_ms=1
        )

    def _get_receipts(payload: GetReceiptsInput) -> GetReceiptsOutput:
        # The real tool, pointed at this fixture's receipts file.
        return get_receipts(payload.model_copy(update={"receipts_path": receipts_file}))

    return RunContext(
        run_id="run-generated",
        writer=JsonlAuditWriter(root / "audit"),
        ingest=_ingest,
        extract=_vision,
        extract_text=_text,
        lookup_vendor=_lookup,
        get_purchase_order=_get_po,
        get_receipts=_get_receipts,
    )


@pytest.fixture
def record(fixture_root: Path) -> InvoiceRecord:
    return InvoiceRecord(source_path=_pdf(fixture_root), created_at=utc_now())


# --- the clean invoice, end to end ------------------------------------------


def test_the_generated_invoice_grounds_its_own_truth_file(fixture_root: Path) -> None:
    """The page and the label agree, which is what makes the rest of this valid.

    If the truth file claimed a total the page does not print, the confidence
    check would refuse the invoice and every routing test below would be
    testing the wrong thing.
    """
    truth = _truth(fixture_root)
    page = _page_text(_pdf(fixture_root))

    assert truth.expected.invoice_number in page
    assert truth.po_number in page
    assert gen.format_amount(truth.expected.total) in page


def test_a_clean_generated_invoice_resolves_its_vendor_and_gathers_the_match(
    record: InvoiceRecord, fixture_root: Path
) -> None:
    """VALIDATED -> VENDOR_RESOLVED -> DUPLICATE_CHECKED -> MATCHED, with real facts."""
    truth = _truth(fixture_root)
    ctx = _context(fixture_root, truth)

    final = run(record, ctx)

    assert final.state is InvoiceState.CLOSED
    assert final.vendor_id == VENDOR_ERP_ID
    assert final.vendor_country == "US"
    assert final.vendor_match is not None
    # No tax id on the page, so the name tier is what resolved it.
    assert final.vendor_match.match_basis is VendorMatchBasis.NAME_EXACT
    assert final.purchase_order is not None
    assert final.purchase_order.po_number == PO_NUMBER
    assert final.receipts is not None
    assert final.receipts.is_empty is False


def test_the_audit_sequence_for_a_generated_invoice(
    record: InvoiceRecord, fixture_root: Path
) -> None:
    """The trail in full. Every row is one thing that happened, in order."""
    ctx = _context(fixture_root, _truth(fixture_root))

    run(record, ctx)

    assert [event.decision for event in ctx.events] == [
        "ingest",
        "read",
        "read",
        "auto_ok",
        "extract",
        "validate",
        "name_exact",
        "resolve_vendor",
        "stub_ok",
        "found",
        "received",
        "stub_ok",
        "stub_ok",
        "stub_ok",
        "stub_ok",
        "stub_ok",
        "stub_ok",
        "stub_ok",
        "stub_ok",
        "stub_ok",
    ]


def test_only_the_moves_carry_a_destination(record: InvoiceRecord, fixture_root: Path) -> None:
    """Following to_state down the file shows the state machine and nothing else."""
    ctx = _context(fixture_root, _truth(fixture_root))

    run(record, ctx)

    moves = [event.to_state for event in ctx.events if event.to_state is not None]
    assert moves[:6] == [
        InvoiceState.INGESTED,
        InvoiceState.EXTRACTED,
        InvoiceState.VALIDATED,
        InvoiceState.VENDOR_RESOLVED,
        InvoiceState.DUPLICATE_CHECKED,
        InvoiceState.MATCHED,
    ]
    assert moves[-1] is InvoiceState.CLOSED


def test_the_chain_verifies(record: InvoiceRecord, fixture_root: Path) -> None:
    ctx = _context(fixture_root, _truth(fixture_root))

    final = run(record, ctx)

    assert JsonlAuditWriter(fixture_root / "audit").verify(str(final.invoice_id)) is True


def test_a_po_belonging_to_another_vendor_is_refused(
    record: InvoiceRecord, fixture_root: Path
) -> None:
    """The same generated invoice, against an order that is not this vendor's."""
    ctx = _context(fixture_root, _truth(fixture_root), po_vendor_erp_id=OTHER_VENDOR_ERP_ID)

    final = run(record, ctx)

    assert final.state is InvoiceState.EXCEPTION
    assert final.receipts is None


def test_the_hidden_text_variant_reaches_the_same_place_on_the_numbers(
    record: InvoiceRecord, fixture_root: Path
) -> None:
    """Its arithmetic is identical to the clean one's, and that is the point.

    Nothing a tolerance can measure is wrong with this document. What is wrong
    with it is a line of white 4pt type instructing the reader to change the
    vendor's bank account - which no rule in the loop looks for yet. The truth
    file says it must reach a person; nothing here does that, and that gap is
    the next guardrail to write, not a failure of this step.
    """
    truth = _truth(fixture_root, GeneratedVariant.HIDDEN_TEXT)
    ctx = _context(fixture_root, truth, variant=GeneratedVariant.HIDDEN_TEXT)

    final = run(record, ctx)

    assert truth.expected_human_review is True
    assert final.state is InvoiceState.CLOSED, "no guardrail reads suspicious_text yet"
    assert truth.hidden_text is not None
    assert truth.hidden_text in _page_text(_pdf(fixture_root, GeneratedVariant.HIDDEN_TEXT))
