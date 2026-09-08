"""Shared fixtures.

Fixtures here build real artefacts - an actual PDF, an actual blurred PNG -
rather than mocking the libraries that read them. A mock of PyMuPDF would prove
that the mock behaves; the point of ``ingest_document`` is that it copes with
files.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from ap_agent.contracts.invoice import InvoiceExtraction, LineItem


@pytest.fixture
def sample_line_items() -> list[LineItem]:
    """Two lines that sum exactly to 300.00."""
    return [
        LineItem(
            description="Widget, 10mm",
            quantity=Decimal(10),
            unit="EA",
            unit_price=Decimal("20.00"),
            extended_price=Decimal("200.00"),
            tax_rate=Decimal("0.20"),
        ),
        LineItem(
            description="Installation labour",
            quantity=Decimal(4),
            unit="HR",
            unit_price=Decimal("25.00"),
            extended_price=Decimal("100.00"),
            tax_rate=Decimal("0.20"),
        ),
    ]


@pytest.fixture
def sample_extraction(sample_line_items: list[LineItem]) -> InvoiceExtraction:
    """An internally consistent extraction: 300.00 + 60.00 = 360.00."""
    return InvoiceExtraction(
        vendor_name="Acme Industrial Supply Ltd",
        vendor_tax_id="GB123456789",
        vendor_email_domain="acme-supply.co.uk",
        invoice_number="INV-2026-00187",
        invoice_date=date(2026, 8, 1),
        due_date=date(2026, 8, 31),
        currency="GBP",
        subtotal=Decimal("300.00"),
        tax_total=Decimal("60.00"),
        total=Decimal("360.00"),
        payment_terms="Net 30",
        po_references=["PO-44821"],
        line_items=sample_line_items,
    )


@pytest.fixture
def born_digital_pdf(tmp_path: Path) -> Path:
    """A two-page PDF with a real text layer, rendered by reportlab."""
    path = tmp_path / "born_digital.pdf"
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for page in (1, 2):
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 760, f"INVOICE INV-2026-00187  page {page}")
        pdf.drawString(72, 740, "Acme Industrial Supply Ltd")
        pdf.drawString(72, 720, "Widget, 10mm    10 EA @ 20.00     200.00")
        pdf.drawString(72, 700, "Total due                          360.00")
        pdf.showPage()
    pdf.save()
    return path


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    """A single-page PDF containing only an image: no text layer."""
    noise = Image.new("L", (400, 560), color=220)
    for x in range(0, 400, 8):
        for y in range(0, 560, 8):
            if (x // 8 + y // 8) % 2 == 0:
                noise.putpixel((x, y), 40)

    path = tmp_path / "scanned.pdf"
    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.drawImage(  # pyright: ignore[reportUnknownMemberType]
        ImageReader(noise), 40, 40, width=500, height=700
    )
    pdf.showPage()
    pdf.save()
    return path


@pytest.fixture
def sharp_png(tmp_path: Path) -> Path:
    """A high-contrast checkerboard: high Laplacian variance."""
    image = Image.new("L", (512, 512), color=255)
    for x in range(512):
        for y in range(512):
            if (x // 8 + y // 8) % 2 == 0:
                image.putpixel((x, y), 0)
    path = tmp_path / "sharp.png"
    image.save(path)
    return path


@pytest.fixture
def blurred_png(sharp_png: Path, tmp_path: Path) -> Path:
    """The same checkerboard through a heavy Gaussian blur: low variance."""
    path = tmp_path / "blurred.png"
    with Image.open(sharp_png) as image:
        image.filter(ImageFilter.GaussianBlur(radius=6)).save(path)
    return path


@pytest.fixture
def corpus_dir(tmp_path: Path, born_digital_pdf: Path, sharp_png: Path) -> Path:
    """A small data tree with one duplicate, for the indexer tests."""
    root = tmp_path / "corpus"
    (root / "synthetic" / "generated").mkdir(parents=True)
    (root / "real").mkdir(parents=True)

    (root / "synthetic" / "generated" / "a.pdf").write_bytes(born_digital_pdf.read_bytes())
    (root / "real" / "copy_of_a.pdf").write_bytes(born_digital_pdf.read_bytes())
    (root / "real" / "scan.png").write_bytes(sharp_png.read_bytes())
    return root
