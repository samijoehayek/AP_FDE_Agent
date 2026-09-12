"""Vendor resolution against the *real* committed master, end to end.

Everything above this file injects a vendor match. These tests let the real
``lookup_vendor`` read ``config/sandbox_vendor_master.yaml``, because two of the
questions here are about that file's contents rather than about the loop:

* A Kaggle invoice's vendor turns out to be in the master. That is not a
  coincidence and it changes where those invoices route - see
  ``test_a_kaggle_invoice_resolves_against_the_seeded_master``.
* The date rule needs a country the master actually holds, and asserting it
  against an injected country would only prove the injection.

No model, no network. Only the two reading seats and QuickBooks are faked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.invoice import InvoiceExtraction, LineItem
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.contracts.vendor import VendorMatchBasis
from ap_agent.loop.runner import RunContext, run
from ap_agent.states.machine import InvoiceState
from ap_agent.tools.compute_extraction_confidence import DateResolutionReason
from ap_agent.tools.extract_invoice_text import ExtractInvoiceTextOutput
from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionOutput
from ap_agent.tools.ingest_document import IngestDocumentOutput

if TYPE_CHECKING:
    from ap_agent.tools.extract_invoice_text import ExtractInvoiceTextInput
    from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionInput

# Invoice 51109301 from the Kaggle corpus, as the extraction seat has actually
# returned it (docs/EXTRACTION_LOG.md). Rebuilt here rather than read from
# data/, which is git-ignored and absent in CI.
KAGGLE_VENDOR = "TechVision Distributors Pvt Ltd"
KAGGLE_TAX_ID = "27AABCT1234F1Z5"

AMBIGUOUS_RENDERING = "09/03/2024"
MARCH = date(2024, 3, 9)
SEPTEMBER = date(2024, 9, 3)


def _kaggle_51109301(invoice_date: date = date(2023, 7, 3)) -> InvoiceExtraction:
    """The real reading: an Indian GST invoice with no purchase-order reference."""
    return InvoiceExtraction(
        vendor_name=KAGGLE_VENDOR,
        vendor_tax_id=KAGGLE_TAX_ID,
        vendor_address="Plot 14, MIDC Industrial Area, Andheri East, Mumbai, Maharashtra",
        bill_to_name="Raj Electronics Pvt Ltd",
        invoice_number="51109301",
        invoice_date=invoice_date,
        currency="INR",
        subtotal=Decimal("1676976.00"),
        tax_total=Decimal("167697.60"),
        total=Decimal("1844673.60"),
        po_references=[],
        line_items=[
            LineItem(
                description="Mechanical Keyboard TKL",
                quantity=Decimal(1),
                unit="EA",
                unit_price=Decimal("1676976.00"),
                extended_price=Decimal("1676976.00"),
            )
        ],
    )


def _unknown_vendor() -> InvoiceExtraction:
    """A supplier nobody has onboarded, which is the NEW_VENDOR case."""
    return InvoiceExtraction(
        vendor_name="Provost Industrial Holdings",
        vendor_tax_id="55-5555555",
        invoice_number="PI-9001",
        invoice_date=date(2026, 5, 4),
        currency="USD",
        subtotal=Decimal("500.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("500.00"),
        po_references=[],
        line_items=[
            LineItem(
                description="Consulting",
                quantity=Decimal(1),
                unit="EA",
                unit_price=Decimal("500.00"),
                extended_price=Decimal("500.00"),
            )
        ],
    )


def _page_for(extraction: InvoiceExtraction, date_rendering: str | None = None) -> str:
    """A text layer that grounds every value the extraction claims."""
    rendering = date_rendering or extraction.invoice_date.isoformat()
    return "\n".join(
        [
            extraction.vendor_name,
            f"GSTIN {extraction.vendor_tax_id}",
            f"Invoice No: {extraction.invoice_number}",
            f"Date of issue: {rendering}",
            f"Subtotal {extraction.subtotal}",
            f"Tax {extraction.tax_total}",
            f"Total {extraction.total} {extraction.currency}",
        ]
    )


def _context(
    tmp_path: Path,
    extraction: InvoiceExtraction,
    *,
    second_read: InvoiceExtraction | None = None,
    date_rendering: str | None = None,
) -> RunContext:
    """Both model seats faked; lookup_vendor is the real one on the real master."""
    page = _page_for(extraction, date_rendering)

    def _ingest(_payload: object) -> IngestDocumentOutput:
        return IngestDocumentOutput(
            sha256="c" * 64,
            byte_size=1024,
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
            second_read=second_read if second_read is not None else extraction,
            model_id="claude-haiku-4-5-20251001",
            prompt_version="extract_text_v1",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
        )

    return RunContext(
        run_id="run-vendor",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        ingest=_ingest,
        extract=_vision,
        extract_text=_text,
    )


@pytest.fixture
def record(tmp_path: Path) -> InvoiceRecord:
    return InvoiceRecord(source_path=tmp_path / "invoice.pdf", created_at=utc_now())


# --- the Kaggle corpus ------------------------------------------------------


def test_a_kaggle_invoice_resolves_against_the_seeded_master(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """51109301's vendor *is* in the master, and it routes to NON_PO.

    Worth stating plainly because it is easy to assume otherwise: the sandbox
    was seeded with vendor names taken from this corpus, so every Kaggle invoice
    carries TechVision's name and GSTIN - which is the first record in
    ``config/sandbox_vendor_master.yaml``. These invoices therefore resolve on
    the tax-id tier and do *not* reach NEW_VENDOR.

    Where they go instead is NON_PO - the Kaggle documents carry no purchase
    order reference at all - and from there onward through the stub edges.
    """
    ctx = _context(tmp_path, _kaggle_51109301())

    final = run(record, ctx)

    assert final.vendor_match is not None
    assert final.vendor_match.match_basis is VendorMatchBasis.TAX_ID_EXACT
    assert final.vendor_id == "58"
    assert final.vendor_country == "IN"
    assert InvoiceState.NON_PO in [event.to_state for event in ctx.events]
    assert final.purchase_order is None


def test_the_kaggle_invoice_fetches_nothing(record: InvoiceRecord, tmp_path: Path) -> None:
    """No PO reference means nothing to fetch, so QuickBooks is never called.

    Which is why this test needs no QuickBooks double at all: reaching for one
    would be a failure, not a fixture.
    """
    ctx = _context(tmp_path, _kaggle_51109301())

    run(record, ctx)

    assert "found" not in [event.decision for event in ctx.events]


# --- a vendor nobody has onboarded ------------------------------------------


def test_an_unknown_vendor_halts_at_new_vendor(record: InvoiceRecord, tmp_path: Path) -> None:
    """Onboarding a supplier is a human process with its own approval.

    No stub edge leaves NEW_VENDOR, so the table refuses, the refusal is
    recorded, and the run ends. That is the designed outcome, not a failure.
    """
    ctx = _context(tmp_path, _unknown_vendor())

    final = run(record, ctx)

    assert final.state is InvoiceState.NEW_VENDOR
    assert final.vendor_match is not None
    assert final.vendor_match.match_basis is VendorMatchBasis.NONE
    assert final.vendor_id is None

    halt = ctx.events[-1]
    assert halt.to_state is None
    assert halt.event_type is AuditEventType.HALT
    assert "NEW_VENDOR" in (halt.decision_basis or "")


def test_the_refusal_is_recorded_before_the_run_ends(record: InvoiceRecord, tmp_path: Path) -> None:
    """A trail with a hole exactly where things stopped looks complete and is not."""
    ctx = _context(tmp_path, _unknown_vendor())

    run(record, ctx)

    decision = next(event for event in ctx.events if event.decision == "vendor_not_found")
    assert decision.to_state is InvoiceState.NEW_VENDOR
    assert "basis=none" in (decision.decision_basis or "")
    assert "Provost Industrial Holdings" in (decision.decision_basis or "")


# --- the date the master settles --------------------------------------------


def test_the_masters_country_settles_a_date_the_window_could_not(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """The whole point of resolving the vendor before the date is settled.

    ``09/03/2024`` is 9 March or 3 September and the page says nothing more. The
    invoice arrived on 1 October 2024, so both readings are still possible and
    the receipt window declines. TechVision is Indian in the master, India reads
    day-first, and the question closes - with the rule that closed it named.
    """
    ctx = _context(
        tmp_path,
        _kaggle_51109301(SEPTEMBER),
        second_read=_kaggle_51109301(MARCH),
        date_rendering=AMBIGUOUS_RENDERING,
    )
    arrived = record.model_copy(update={"received_at": datetime(2024, 10, 1, 12, 0, tzinfo=UTC)})

    final = run(arrived, ctx)

    assert final.vendor_country == "IN"
    assert final.invoice_date_resolved == MARCH
    assert final.date_resolution_reason is DateResolutionReason.VENDOR_LOCALE


def test_the_date_is_still_open_when_the_vendor_step_begins(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """Otherwise the test above would pass for the wrong reason.

    If the receipt window had already settled it there would be nothing left for
    the master's country to do.
    """
    ctx = _context(
        tmp_path,
        _kaggle_51109301(SEPTEMBER),
        second_read=_kaggle_51109301(MARCH),
        date_rendering=AMBIGUOUS_RENDERING,
    )
    arrived = record.model_copy(update={"received_at": datetime(2024, 10, 1, 12, 0, tzinfo=UTC)})

    part_way = run(arrived, ctx, max_steps=3)

    assert part_way.state is InvoiceState.VALIDATED
    assert part_way.date_is_open is True
    assert set(part_way.date_candidates) == {MARCH, SEPTEMBER}
