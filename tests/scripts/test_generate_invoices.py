"""The generated fixture is only useful if its labels are true.

Every test here runs the real renderer against a small seeded fixture in
``tmp_path`` and reads the PDFs back with pymupdf. Nothing is mocked: a mock of
reportlab would prove the mock draws, and the properties under test - that two
runs produce the same bytes, that the planted string is in the text layer, that
the printed total is the total the truth file claims - are properties of actual
files.

No network, no model, no QuickBooks, and nothing is written under ``data/``.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pymupdf
import pytest

from ap_agent.contracts.enums import ReasonCode
from ap_agent.contracts.generated import (
    ExpectedMatch,
    GeneratedInvoiceTruth,
    GeneratedVariant,
)
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.errors import APAgentError
from ap_agent.states.machine import InvoiceState
from ap_agent.tools.pymupdf_types import PdfDocument
from scripts import generate_invoices as gen

GROUPED_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
"""Any IBAN-shaped token, run together or grouped in fours as banks print them.

The clean path must not contain one anywhere."""


# --- a small seeded fixture, in the shape of the real files ------------------

VENDOR_MASTER = """
version: "v1"
vendors:
  - display_name: "Northwind Peripherals Inc"
    erp_id: "62"
    currency: "USD"
    tax_id: "27-6653019"
    country: "US"
    address:
      - "6600 Cedar Bluff Parkway"
      - "Saint Paul, MN 55114"
  - display_name: "Vasanth Industrial Tools"
    erp_id: "67"
    currency: "INR"
    tax_id: "33AAHCV2298N1ZR"
    country: "IN"
    address:
      - "No. 7, Ambattur Industrial Estate"
      - "Chennai 600058"
"""


def _manifest() -> dict[str, Any]:
    """Three orders: fully received, partially received, and not received."""
    return {
        "home_currency": "USD",
        "purchase_orders": [
            {
                "qbo_id": "145",
                "doc_number": "AP-TEST-001",
                "vendor": "Northwind Peripherals Inc",
                "vendor_qbo_id": "62",
                "currency": "USD",
                "order_date": "2026-07-27",
                "total": "6400.00",
                "lines": [
                    {"item": "Widget", "qty": "4", "unit_price": "1500.00", "amount": "6000.00"},
                    {"item": "Gasket", "qty": "2", "unit_price": "200.00", "amount": "400.00"},
                ],
            },
            {
                "qbo_id": "146",
                "doc_number": "AP-TEST-002",
                "vendor": "Vasanth Industrial Tools",
                "vendor_qbo_id": "67",
                "currency": "USD",
                "order_date": "2026-08-05",
                "total": "3400.00",
                "lines": [
                    {"item": "Spindle", "qty": "2", "unit_price": "1500.00", "amount": "3000.00"},
                    {"item": "Bearing", "qty": "2", "unit_price": "200.00", "amount": "400.00"},
                ],
            },
            {
                "qbo_id": "147",
                "doc_number": "AP-TEST-003",
                "vendor": "Northwind Peripherals Inc",
                "vendor_qbo_id": "62",
                "currency": "USD",
                "order_date": "2026-08-11",
                "total": "4500.00",
                "lines": [
                    {"item": "Chassis", "qty": "3", "unit_price": "1500.00", "amount": "4500.00"}
                ],
            },
        ],
    }


def _receipts() -> dict[str, Any]:
    return {
        "receipts": [
            {
                "receipt_id": "GR-AP-TEST-001",
                "po_number": "AP-TEST-001",
                "status": "full",
                "received_on": "2026-07-27",
                "lines": [
                    {"item": "Widget", "qty_ordered": "4", "qty_received": "4"},
                    {"item": "Gasket", "qty_ordered": "2", "qty_received": "2"},
                ],
            },
            {
                "receipt_id": "GR-AP-TEST-002",
                "po_number": "AP-TEST-002",
                "status": "partial",
                "received_on": "2026-08-05",
                # Bearing arrived not at all: the clean invoice must leave it off.
                "lines": [
                    {"item": "Spindle", "qty_ordered": "2", "qty_received": "1"},
                    {"item": "Bearing", "qty_ordered": "2", "qty_received": "0"},
                ],
            },
            {
                "receipt_id": "GR-AP-TEST-003",
                "po_number": "AP-TEST-003",
                "status": "none",
                "received_on": "2026-08-11",
                "lines": [{"item": "Chassis", "qty_ordered": "3", "qty_received": "0"}],
            },
        ]
    }


@pytest.fixture
def seed_paths(tmp_path: Path) -> gen.SeedPaths:
    """The three inputs, written into tmp_path. Nothing touches data/."""
    manifest = tmp_path / "seed_manifest.json"
    receipts = tmp_path / "receipts.json"
    master = tmp_path / "vendor_master.yaml"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    receipts.write_text(json.dumps(_receipts()), encoding="utf-8")
    master.write_text(VENDOR_MASTER, encoding="utf-8")
    return gen.SeedPaths(manifest=manifest, receipts=receipts, vendor_master=master)


@pytest.fixture
def generated(tmp_path: Path, seed_paths: gen.SeedPaths) -> Path:
    """The whole fixture, rendered once into tmp_path."""
    out = tmp_path / "invoices"
    gen.generate(out, paths=seed_paths)
    return out


def _text_of(pdf: Path) -> str:
    """The text layer, read the way the second reading reads it.

    Cast to the protocol in ``pymupdf_types`` for the same reason the tools do:
    PyMuPDF ships incomplete annotations, and the alternative is an ignore at
    every call site.
    """
    with cast("PdfDocument", pymupdf.open(pdf)) as document:
        return "\n".join(document[index].get_text("text") for index in range(document.page_count))


def _truth_files(out: Path) -> list[Path]:
    return sorted(out.rglob("truth.json"))


def _load_truth(path: Path) -> GeneratedInvoiceTruth:
    return GeneratedInvoiceTruth.model_validate_json(path.read_text(encoding="utf-8"))


def _pdf_beside(truth_path: Path) -> Path:
    return truth_path.parent / "invoice.pdf"


# --- determinism ------------------------------------------------------------


def test_generating_twice_gives_identical_bytes(tmp_path: Path, seed_paths: gen.SeedPaths) -> None:
    """Hashes are document identity. A generator whose bytes move breaks it.

    ``documents`` is keyed by SHA-256 and the duplicate control compares hashes,
    so a regeneration that changed every byte would arrive as a corpus of new
    documents rather than the same fixture.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    gen.generate(first, paths=seed_paths)
    gen.generate(second, paths=seed_paths)

    files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    assert files, "nothing was generated"
    for relative in files:
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative


def test_rerunning_in_place_leaves_the_same_bytes(
    tmp_path: Path, seed_paths: gen.SeedPaths
) -> None:
    """Overwriting is safe precisely because the output does not change."""
    out = tmp_path / "invoices"
    gen.generate(out, paths=seed_paths)
    before = {p: p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}
    gen.generate(out, paths=seed_paths)
    after = {p: p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}
    assert before == after


def test_no_date_on_the_page_comes_from_the_clock(generated: Path) -> None:
    """Every date is derived from the purchase order, three days on."""
    truth = _load_truth(generated / "AP-TEST-001" / "clean" / "truth.json")
    assert truth.expected.invoice_date.isoformat() == "2026-07-30"
    assert truth.expected.due_date.isoformat() == "2026-08-29"


# --- identity ---------------------------------------------------------------


def test_every_invoice_number_is_unique(generated: Path) -> None:
    """Six variants of one order from one vendor are six documents.

    The database enforces ``(vendor_id, invoice_number)`` uniqueness, so a
    repeated number anywhere in the fixture would make the corpus unloadable.
    """
    numbers = [_load_truth(p).expected.invoice_number for p in _truth_files(generated)]
    assert len(numbers) == len(set(numbers)), sorted(numbers)


def test_every_pdf_has_distinct_bytes(generated: Path) -> None:
    digests = [hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(generated.rglob("*.pdf"))]
    assert len(digests) == len(set(digests))


def test_the_manifest_indexes_every_document(generated: Path) -> None:
    manifest: dict[str, Any] = json.loads((generated / "manifest.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = manifest["invoices"]
    pdfs = sorted(generated.rglob("*.pdf"))

    assert len(rows) == len(pdfs)
    assert {row["file"] for row in rows} == {str(p.relative_to(generated)) for p in pdfs}
    assert len({row["sha256"] for row in rows}) == len(rows)
    for row in rows:
        on_disk = hashlib.sha256((generated / row["file"]).read_bytes()).hexdigest()
        assert row["sha256"] == on_disk, row["file"]


def test_the_layout_on_disk_is_po_then_variant(generated: Path) -> None:
    for truth_path in _truth_files(generated):
        truth = _load_truth(truth_path)
        assert truth_path.parent.name == truth.variant.value
        assert truth_path.parent.parent.name == truth.po_number
        assert _pdf_beside(truth_path).is_file()


# --- the truth files --------------------------------------------------------


def test_every_truth_file_validates_and_is_complete(generated: Path) -> None:
    paths = _truth_files(generated)
    assert len(paths) == 3 * len(GeneratedVariant)
    for path in paths:
        truth = _load_truth(path)
        assert truth.generator_version == "gen_v1"
        assert truth.vendor_country in {"US", "IN"}


def test_an_extraction_built_from_truth_passes_the_arithmetic_validator(generated: Path) -> None:
    """The generator must agree with the contract about its own page.

    ``InvoiceExtraction`` computes the arithmetic flags itself at construction.
    If the printed subtotal, tax and total do not reconcile, or a line does not
    extend, this is where it shows - and it means the generator is wrong, not
    the document.
    """
    for path in _truth_files(generated):
        truth = _load_truth(path)
        extraction = InvoiceExtraction(**truth.expected.model_dump())
        assert extraction.arithmetic_flags == [], (path, extraction.arithmetic_flags)
        assert extraction.is_arithmetically_consistent


def test_money_is_serialised_as_strings_never_floats(generated: Path) -> None:
    """A float in a truth file would disagree with the page it describes."""
    for path in _truth_files(generated):
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        expected: dict[str, Any] = raw["expected"]
        for key in ("subtotal", "tax_total", "total"):
            assert isinstance(expected[key], str), (path, key)
        for line in expected["line_items"]:
            for key in ("quantity", "unit_price", "extended_price"):
                assert isinstance(line[key], str), (path, key)


def test_only_clean_has_no_planted_defect(generated: Path) -> None:
    for path in _truth_files(generated):
        truth = _load_truth(path)
        if truth.variant is GeneratedVariant.CLEAN:
            assert truth.planted_defect is None
        else:
            assert truth.planted_defect, path


# --- what each variant did to the page --------------------------------------


def _truth_for(out: Path, po: str, variant: GeneratedVariant) -> GeneratedInvoiceTruth:
    return _load_truth(out / po / variant.value / "truth.json")


def test_clean_bills_only_what_was_received(generated: Path) -> None:
    """The honest invoice against a part-delivered order bills the part.

    AP-TEST-002 received 1 of 2 Spindles and none of the Bearings, so the clean
    invoice is one line of one Spindle. A line nothing arrived against is left
    off rather than billed at zero.
    """
    truth = _truth_for(generated, "AP-TEST-002", GeneratedVariant.CLEAN)
    assert [line.description for line in truth.expected.line_items] == ["Spindle"]
    assert truth.expected.line_items[0].quantity == Decimal(1)
    assert truth.expected_match is ExpectedMatch.MATCHED
    assert truth.expected_reason_codes == []
    assert truth.expected_human_review is False


def test_clean_against_an_unreceived_order_is_still_an_exception(generated: Path) -> None:
    """A correct invoice for goods that have not arrived is an exception.

    Nothing is wrong with the document. It bills the full ordered quantity at
    the ordered price and the arithmetic reconciles - and it must still not
    post, because the third leg of the match is missing.
    """
    truth = _truth_for(generated, "AP-TEST-003", GeneratedVariant.CLEAN)
    assert truth.expected.line_items[0].quantity == Decimal(3)
    assert truth.expected_match is ExpectedMatch.EXCEPTION
    assert truth.expected_reason_codes == [ReasonCode.RECEIPT_MISSING]
    assert truth.expected_human_review is True
    assert truth.planted_defect is None


def test_price_plus_3pct_exceeds_the_two_percent_band(generated: Path) -> None:
    clean = _truth_for(generated, "AP-TEST-001", GeneratedVariant.CLEAN)
    uplifted = _truth_for(generated, "AP-TEST-001", GeneratedVariant.PRICE_PLUS_3PCT)

    pairs = zip(clean.expected.line_items, uplifted.expected.line_items, strict=True)
    changed = [(before, after) for before, after in pairs if before.unit_price != after.unit_price]
    assert len(changed) == 1, "exactly one line carries the defect"
    before, after = changed[0]
    variance = (after.unit_price - before.unit_price) / before.unit_price
    assert variance > Decimal("0.02")
    assert after.extended_price == after.quantity * after.unit_price
    assert uplifted.expected_match is ExpectedMatch.EXCEPTION
    assert uplifted.expected_reason_codes == [ReasonCode.PRICE_OVER_TOLERANCE]


def test_the_defect_lands_on_the_largest_line(generated: Path) -> None:
    """A price variance on the cheapest line tests arithmetic and nothing else."""
    clean = _truth_for(generated, "AP-TEST-001", GeneratedVariant.CLEAN)
    uplifted = _truth_for(generated, "AP-TEST-001", GeneratedVariant.PRICE_PLUS_3PCT)
    largest = max(clean.expected.line_items, key=lambda line: line.extended_price)
    pairs = zip(clean.expected.line_items, uplifted.expected.line_items, strict=True)
    changed = next(after for before, after in pairs if before.unit_price != after.unit_price)
    assert changed.description == largest.description


def test_qty_over_received_bills_exactly_two_units_over(generated: Path) -> None:
    """Over-billing tolerance is 0%, so two units over is unambiguous."""
    received = {"AP-TEST-001": Decimal(4), "AP-TEST-002": Decimal(1), "AP-TEST-003": Decimal(0)}
    for po_number, arrived in received.items():
        clean = _truth_for(generated, po_number, GeneratedVariant.CLEAN)
        over = _truth_for(generated, po_number, GeneratedVariant.QTY_OVER_RECEIVED)
        largest = max(clean.expected.line_items, key=lambda line: line.extended_price)
        billed = next(
            line for line in over.expected.line_items if line.description == largest.description
        )
        assert billed.quantity == arrived + 2, po_number
        assert billed.extended_price == billed.quantity * billed.unit_price
        assert ReasonCode.QUANTITY_OVER_TOLERANCE in over.expected_reason_codes


@pytest.mark.parametrize(
    ("variant", "amount", "reasons"),
    [
        (GeneratedVariant.FREIGHT_SMALL, Decimal("25.00"), []),
        (GeneratedVariant.FREIGHT_LARGE, Decimal("120.00"), [ReasonCode.LINE_NOT_ON_PO]),
    ],
)
def test_a_freight_line_is_added_with_no_po_reference(
    generated: Path,
    variant: GeneratedVariant,
    amount: Decimal,
    reasons: list[ReasonCode],
) -> None:
    """$25 passes the $50 unmatched-charge rule; $120 does not.

    Two sizes on purpose. One would only ever prove the rule fires, never that
    it lets an ordinary carriage charge through.
    """
    truth = _truth_for(generated, "AP-TEST-001", variant)
    freight = [line for line in truth.expected.line_items if line.description == "Freight"]
    assert len(freight) == 1
    assert freight[0].extended_price == amount
    assert freight[0].unit_price == amount
    assert freight[0].quantity == Decimal(1)
    assert freight[0].po_line_ref is None
    assert truth.expected_reason_codes == reasons
    assert truth.expected.subtotal == sum(
        (line.extended_price for line in truth.expected.line_items), Decimal(0)
    )


def test_hidden_text_holds_up_the_document_without_a_numeric_defect(generated: Path) -> None:
    """The numbers match. The document must not post anyway.

    ``expected_human_review`` is a separate field from ``expected_match`` for
    exactly this case: nothing a tolerance can measure is wrong with the page.
    """
    clean = _truth_for(generated, "AP-TEST-001", GeneratedVariant.CLEAN)
    hidden = _truth_for(generated, "AP-TEST-001", GeneratedVariant.HIDDEN_TEXT)

    assert hidden.expected.total == clean.expected.total
    assert hidden.expected_match is ExpectedMatch.MATCHED
    assert hidden.expected_human_review is True
    assert hidden.expected_reason_codes == [ReasonCode.SUSPICIOUS_DOCUMENT_CONTENT]
    assert hidden.hidden_text == gen.HIDDEN_TEXT


def test_an_unreceived_order_holds_every_variant(generated: Path) -> None:
    """Nothing arrived, so it is a property of the order, not of the defect."""
    for variant in GeneratedVariant:
        truth = _truth_for(generated, "AP-TEST-003", variant)
        assert truth.expected_match is ExpectedMatch.EXCEPTION, variant
        assert truth.expected_reason_codes[0] is ReasonCode.RECEIPT_MISSING, variant
        assert truth.expected_human_review is True, variant


def test_every_expected_reason_code_is_in_the_existing_enum(generated: Path) -> None:
    """The vocabulary is closed. A fixture may not widen it."""
    for path in _truth_files(generated):
        for code in _load_truth(path).expected_reason_codes:
            assert code in set(ReasonCode), (path, code)


# --- the text layer ---------------------------------------------------------


def test_the_page_carries_the_fields_the_truth_file_claims(generated: Path) -> None:
    """Read back with pymupdf, which is what the second reading uses."""
    for truth_path in _truth_files(generated):
        truth = _load_truth(truth_path)
        text = _text_of(_pdf_beside(truth_path))
        expected = truth.expected

        assert expected.invoice_number in text, truth_path
        assert truth.po_number in text, truth_path
        # The name as printed - which on lookalike_vendor is deliberately not the master's.
        assert expected.vendor_name in text, truth_path
        assert gen.format_amount(expected.total) in text, truth_path
        assert gen.format_date(expected.invoice_date) in text, truth_path
        assert gen.format_date(expected.due_date) in text, truth_path
        assert expected.currency in text, truth_path
        assert "PO Number:" in text, truth_path
        assert expected.payment_terms in text, truth_path


def test_the_date_is_written_unambiguously(generated: Path) -> None:
    """Day first with the month named, so no locale rule has to guess.

    Deliberate. A slash date would make every match test also a test of the
    date-resolution rule, so a failure would have two candidate causes. The
    ambiguous-date path is exercised by the real corpus, which has genuine
    slash dates on it.
    """
    text = _text_of(generated / "AP-TEST-001" / "clean" / "invoice.pdf")
    assert "30 Jul 2026" in text
    assert not re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", text)


def test_the_planted_string_is_in_the_hidden_text_layer(generated: Path) -> None:
    """Invisible to a reader and to the vision seat; plainly there in the text.

    That disagreement between the two readings is the signal: the text model
    sees an instruction the vision model cannot, and a check scoring agreement
    between them is what notices.
    """
    text = _text_of(generated / "AP-TEST-001" / "hidden_text" / "invoice.pdf")
    assert gen.HIDDEN_TEXT in text


def test_no_other_variant_carries_the_planted_string(generated: Path) -> None:
    for truth_path in _truth_files(generated):
        truth = _load_truth(truth_path)
        if truth.variant is GeneratedVariant.HIDDEN_TEXT:
            continue
        assert gen.HIDDEN_TEXT not in _text_of(_pdf_beside(truth_path)), truth_path


def test_no_clean_path_document_contains_anything_iban_shaped(generated: Path) -> None:
    """Rule 2 of CLAUDE.md, asserted against the rendered page.

    The remit-to block prints a postal address and nothing else. The only
    documents with an account number on them are the adversarial ones, whose
    whole purpose is to be caught - and every one of them prints the same
    published test IBAN. Grouped renderings (``GB29 NWBK ...``) are matched too,
    so spacing cannot slip one past.
    """
    carrying = {
        GeneratedVariant.HIDDEN_TEXT,
        GeneratedVariant.INSTRUCTION_TEXT,
        GeneratedVariant.OFFPAGE_TEXT,
        GeneratedVariant.REMIT_MISMATCH,
    }
    for truth_path in _truth_files(generated):
        truth = _load_truth(truth_path)
        pdf = _pdf_beside(truth_path)
        text = _text_of(pdf) + " " + _unclipped_text_of(pdf)
        found = {token.replace(" ", "") for token in GROUPED_IBAN.findall(text)}
        if truth.variant in carrying:
            assert found == {"GB29NWBK60161331926819"}, (truth_path, found)
        else:
            assert found == set(), (truth_path, found)


def test_the_remit_to_block_is_a_postal_address_only(generated: Path) -> None:
    text = _text_of(generated / "AP-TEST-001" / "clean" / "invoice.pdf")
    assert "Remit To" in text
    for forbidden in ("IBAN", "SWIFT", "BIC", "Sort Code", "Account Number", "Routing"):
        assert forbidden.lower() not in text.lower(), forbidden


# --- selection and failure modes --------------------------------------------


def test_only_renders_one_purchase_order(tmp_path: Path, seed_paths: gen.SeedPaths) -> None:
    out = tmp_path / "one"
    rows = gen.generate(out, only="AP-TEST-002", paths=seed_paths)
    assert {row["po_number"] for row in rows} == {"AP-TEST-002"}
    assert len(rows) == len(GeneratedVariant)


def test_variants_renders_a_subset(tmp_path: Path, seed_paths: gen.SeedPaths) -> None:
    out = tmp_path / "subset"
    rows = gen.generate(
        out,
        variants=(GeneratedVariant.CLEAN, GeneratedVariant.HIDDEN_TEXT),
        paths=seed_paths,
    )
    assert {row["variant"] for row in rows} == {"clean", "hidden_text"}


def test_planning_writes_nothing(tmp_path: Path, seed_paths: gen.SeedPaths) -> None:
    """--dry-run plans the real work rather than describing it."""
    out = tmp_path / "planned"
    renderings = gen.plan(paths=seed_paths)
    assert len(renderings) == 3 * len(GeneratedVariant)
    assert not out.exists()


def test_an_unknown_purchase_order_says_which_ones_exist(seed_paths: gen.SeedPaths) -> None:
    with pytest.raises(APAgentError, match="AP-TEST-001"):
        gen.plan(only="AP-SEED-999", paths=seed_paths)


def test_an_unknown_variant_is_refused(seed_paths: gen.SeedPaths) -> None:
    del seed_paths
    with pytest.raises(APAgentError, match="unknown variants"):
        gen.parse_variants("clean,not_a_variant")


def test_an_unknown_layout_is_refused(tmp_path: Path, seed_paths: gen.SeedPaths) -> None:
    with pytest.raises(APAgentError, match="unknown layout"):
        gen.generate(tmp_path / "out", layout="z", paths=seed_paths)


def test_a_vendor_missing_from_the_master_fails_loudly(
    tmp_path: Path, seed_paths: gen.SeedPaths
) -> None:
    """Better a stopped run than an invented country on a rendered page."""
    manifest = _manifest()
    manifest["purchase_orders"][0]["vendor"] = "Nobody In The Master Ltd"
    seed_paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(APAgentError, match="Nobody In The Master Ltd"):
        gen.plan(paths=seed_paths)
    assert not (tmp_path / "invoices").exists()


def test_a_receipt_that_does_not_cover_an_order_fails(seed_paths: gen.SeedPaths) -> None:
    receipts = _receipts()
    receipts["receipts"] = receipts["receipts"][1:]
    seed_paths.receipts.write_text(json.dumps(receipts), encoding="utf-8")

    with pytest.raises(APAgentError, match="no receipt"):
        gen.plan(paths=seed_paths)


def test_a_missing_seed_file_says_to_seed_first(tmp_path: Path) -> None:
    paths = gen.SeedPaths(manifest=tmp_path / "nope.json", receipts=tmp_path / "also-nope.json")
    with pytest.raises(APAgentError, match="just seed"):
        gen.plan(paths=paths)


# --- formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "printed"),
    [
        (Decimal("0.00"), "0.00"),
        (Decimal("25.00"), "25.00"),
        (Decimal("1234.50"), "1,234.50"),
        (Decimal("651670.00"), "651,670.00"),
    ],
)
def test_amounts_print_with_thousands_grouping(amount: Decimal, printed: str) -> None:
    assert gen.format_amount(amount) == printed


@pytest.mark.parametrize(
    ("value", "printed"),
    [
        (Decimal(1), "1"),
        (Decimal("11.000000"), "11"),
        (Decimal("2.5"), "2.5"),
    ],
)
def test_quantities_print_without_trailing_zeroes(value: Decimal, printed: str) -> None:
    assert gen.format_quantity(value) == printed


# --- the adversarial variants ------------------------------------------------

ADVERSARIAL = (
    GeneratedVariant.HIDDEN_TEXT,
    GeneratedVariant.INSTRUCTION_TEXT,
    GeneratedVariant.OFFPAGE_TEXT,
    GeneratedVariant.LOOKALIKE_VENDOR,
    GeneratedVariant.REMIT_MISMATCH,
)


def _unclipped_text_of(pdf: Path) -> str:
    """Every span, including the ones outside the page box - what intake reads."""
    with cast("PdfDocument", pymupdf.open(pdf)) as document:
        page = document[0]
        flags = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_MEDIABOX_CLIP  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return str(page.get_text("text", flags=flags, clip=pymupdf.INFINITE_RECT()))  # pyright: ignore[reportUnknownArgumentType]


def test_every_truth_file_is_the_current_label_shape(generated: Path) -> None:
    for path in _truth_files(generated):
        assert _load_truth(path).truth_version == "truth_v2", path


def test_every_adversarial_variant_declares_where_it_stops_and_why(generated: Path) -> None:
    """A halt and a flag on all five, on every order - including the unreceived one.

    They stop before the match, so the order's own state never decides where.
    """
    expected = {
        GeneratedVariant.HIDDEN_TEXT: (InvoiceState.NEEDS_HUMAN_EXTRACTION, "near_white_text"),
        GeneratedVariant.OFFPAGE_TEXT: (InvoiceState.NEEDS_HUMAN_EXTRACTION, "offpage_text"),
        GeneratedVariant.INSTRUCTION_TEXT: (
            InvoiceState.NEEDS_HUMAN_EXTRACTION,
            "suspicious_text",
        ),
        GeneratedVariant.LOOKALIKE_VENDOR: (InvoiceState.NEW_VENDOR, "vendor_not_found"),
        GeneratedVariant.REMIT_MISMATCH: (InvoiceState.NEW_VENDOR, "remit_to_mismatch"),
    }
    for path in _truth_files(generated):
        truth = _load_truth(path)
        if truth.variant in expected:
            assert (truth.expected_halt_state, truth.expected_flag) == expected[truth.variant]
            assert truth.expected_human_review is True, path
        else:
            assert truth.expected_flag is None, path


def test_a_held_match_stops_at_exception_and_a_clean_one_does_not_stop(generated: Path) -> None:
    for path in _truth_files(generated):
        truth = _load_truth(path)
        if truth.variant in ADVERSARIAL:
            continue
        if truth.expected_match is ExpectedMatch.EXCEPTION:
            assert truth.expected_halt_state is InvoiceState.EXCEPTION, path
        else:
            assert truth.expected_halt_state is None, path


def test_the_adversarial_variants_bill_the_honest_invoice(generated: Path) -> None:
    """One defect each, and it is never in the numbers."""
    clean = _truth_for(generated, "AP-TEST-001", GeneratedVariant.CLEAN)
    for variant in ADVERSARIAL:
        truth = _truth_for(generated, "AP-TEST-001", variant)
        assert truth.expected.line_items == clean.expected.line_items, variant
        assert truth.expected.total == clean.expected.total, variant


def test_instruction_text_is_visible_on_the_page(generated: Path) -> None:
    """Inside the page box, so both readers see it and the output filter must catch it."""
    text = _text_of(generated / "AP-TEST-001" / "instruction_text" / "invoice.pdf")
    assert gen.INSTRUCTION_TEXT in text


def test_offpage_text_is_in_the_file_and_not_on_the_page(generated: Path) -> None:
    pdf = generated / "AP-TEST-001" / "offpage_text" / "invoice.pdf"
    assert gen.INSTRUCTION_TEXT not in _text_of(pdf), "nothing that clips to the page sees it"
    assert gen.INSTRUCTION_TEXT in _unclipped_text_of(pdf), "but it is in the content stream"


def test_the_lookalike_is_one_character_off_with_a_different_tax_id(generated: Path) -> None:
    truth = _truth_for(generated, "AP-TEST-001", GeneratedVariant.LOOKALIKE_VENDOR)
    real, printed = truth.vendor_name, truth.expected.vendor_name
    assert printed != real
    assert len(printed) == len(real) + 1
    assert any(printed[:i] + printed[i + 1 :] == real for i in range(len(printed)))

    text = _text_of(generated / "AP-TEST-001" / "lookalike_vendor" / "invoice.pdf")
    assert printed in text
    assert real not in text, "the real name appears nowhere on the page"


@pytest.mark.parametrize(
    ("tax_id", "changed"),
    [("84-1938472", "84-1938473"), ("27AABCT1234F1Z5", "27AABCT1234F1Z6"), ("X1Z", "X1A")],
)
def test_the_lookalike_tax_id_changes_only_its_last_character(tax_id: str, changed: str) -> None:
    assert gen.lookalike_tax_id(tax_id) == changed


def test_only_remit_mismatch_prints_an_account_in_the_remit_block(generated: Path) -> None:
    for path in _truth_files(generated):
        truth = _load_truth(path)
        text = _text_of(_pdf_beside(path))
        if truth.variant is GeneratedVariant.REMIT_MISMATCH:
            assert gen.REMIT_MISMATCH_ACCOUNT in text, path
        else:
            assert gen.REMIT_MISMATCH_ACCOUNT not in text, path
