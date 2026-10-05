"""The CLI surface: implemented commands work, stubbed ones fail visibly."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ap_agent import __version__
from ap_agent import cli as cli_module
from ap_agent.cli import app
from ap_agent.contracts.enums import ReasonCode, SuggestedAction, SuggestedResolver
from ap_agent.contracts.exceptions import ExceptionClassification
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.loop import runner as runner_module
from ap_agent.states.machine import InvoiceState
from ap_agent.tools.compute_extraction_confidence import ComputeExtractionConfidenceInput
from ap_agent.tools.extract_invoice_text import ExtractInvoiceTextOutput
from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionOutput

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_ingest_prints_json(born_digital_pdf: Path) -> None:
    result = runner.invoke(app, ["ingest", str(born_digital_pdf), "--no-sharpness"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["page_count"] == 2
    assert payload["is_born_digital"] is True
    assert len(payload["sha256"]) == 64


def test_ingest_reports_a_bad_document_without_a_traceback(tmp_path: Path) -> None:
    """Not an error any more: an unrecognised file is a flag a person reads."""
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"not a document at all")
    result = runner.invoke(app, ["ingest", str(path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [flag["check"] for flag in payload["flags"]] == ["file_type"]


def test_ingest_reports_an_unreadable_document_without_a_traceback(tmp_path: Path) -> None:
    path = tmp_path / "truncated.pdf"
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog")
    result = runner.invoke(app, ["ingest", str(path)])
    assert result.exit_code == 1
    assert "error:" in result.output


def test_states_graph_emits_mermaid() -> None:
    result = runner.invoke(app, ["states", "graph"])
    assert result.exit_code == 0
    assert result.output.startswith("stateDiagram-v2")


def test_states_list_covers_every_state() -> None:
    result = runner.invoke(app, ["states", "list"])
    assert result.exit_code == 0
    for state in InvoiceState:
        assert state.value in result.output


def test_states_check_resolves_a_legal_transition() -> None:
    result = runner.invoke(app, ["states", "check", "RECEIVED", "ingest"])
    assert result.exit_code == 0
    assert result.output.strip() == "INGESTED"


def test_states_check_rejects_an_illegal_transition() -> None:
    result = runner.invoke(app, ["states", "check", "RECEIVED", "approve"])
    assert result.exit_code == 1


def test_run_processes_an_invoice_and_reports_where_it_stopped(
    born_digital_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extraction seat is replaced, so this never reaches the API.

    The vendor is a real one from the committed master, and the invoice cites no
    purchase order - so the run resolves the vendor against a local file and
    routes down the NON_PO path. Nothing here touches QuickBooks:
    ``get_purchase_order`` is never reached, because an invoice with no PO
    reference has nothing to fetch.
    """
    extraction = InvoiceExtraction.model_validate(
        {
            "vendor_name": "TechVision Distributors Pvt Ltd",
            "vendor_tax_id": "27AABCT1234F1Z5",
            "invoice_number": "INV-1",
            "invoice_date": date(2026, 1, 1),
            "currency": "USD",
            "subtotal": Decimal("100.00"),
            "tax_total": Decimal("20.00"),
            "total": Decimal("120.00"),
            "line_items": [
                {
                    "description": "Widget",
                    "quantity": Decimal(1),
                    "unit": "EA",
                    "unit_price": Decimal("100.00"),
                    "extended_price": Decimal("100.00"),
                }
            ],
        }
    )

    def _extract(_payload: object) -> ExtractInvoiceVisionOutput:
        return ExtractInvoiceVisionOutput(
            extraction=extraction,
            model_id="claude-sonnet-5",
            prompt_version="extract_v1",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
        )

    # A text layer that grounds every claimed value, because the confidence
    # check between the two seats is left real.
    page = (
        "TechVision Distributors Pvt Ltd\n"
        "Invoice No: INV-1\n"
        "Date of issue: 2026-01-01\n"
        "Subtotal 100.00\n"
        "Tax 20.00\n"
        "Total 120.00 USD"
    )

    def _extract_text(_payload: object) -> ExtractInvoiceTextOutput:
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

    # Both seats are replaced. The confidence check between them is left real:
    # it is pure code, so faking it would only test the fake.
    monkeypatch.setattr(runner_module, "extract_invoice_vision", _extract)
    monkeypatch.setattr(runner_module, "extract_invoice_text", _extract_text)
    result = runner.invoke(
        app,
        ["run", str(born_digital_pdf), "--audit-dir", str(tmp_path / "audit")],
    )
    assert result.exit_code == 0, result.output
    assert "final state : CLOSED" in result.output
    # Fourteen moves. Twenty-one rows: intake is one move and two rows, the
    # extraction step one move and six (two readings, the output filter's
    # verdict on each, the score, the decision), resolving the vendor one and two.
    assert "steps       : 14" in result.output
    assert "audit rows  : 21" in result.output
    assert "chain intact: True" in result.output


# --- confidence, replayed from saved readings --------------------------------


def _saved_readings(tmp_path: Path, *, ambiguous: bool = False) -> Path:
    """Write a readings file of the shape ``--out`` produces."""
    rendering = "09/03/2024" if ambiguous else "2024-03-09"
    page = "\n".join(
        [
            "TechVision Distributors Pvt Ltd",
            "Invoice No: 51109305",
            f"Date of issue: {rendering}",
            "Subtotal 2023625.00",
            "Tax 202362.50",
            "Total 2225987.50 INR",
        ]
    )
    extraction = InvoiceExtraction.model_validate(
        {
            "vendor_name": "TechVision Distributors Pvt Ltd",
            "invoice_number": "51109305",
            "invoice_date": date(2024, 3, 9),
            "currency": "INR",
            "subtotal": Decimal("2023625.00"),
            "tax_total": Decimal("202362.50"),
            "total": Decimal("2225987.50"),
        }
    )
    readings = ComputeExtractionConfidenceInput(
        primary=extraction, secondary=extraction, raw_text=page
    )
    target = tmp_path / "readings.json"
    target.write_text(
        json.dumps({"readings": readings.model_dump(mode="json")}, indent=2), encoding="utf-8"
    )
    return target


def test_confidence_can_be_replayed_without_calling_a_model(tmp_path: Path) -> None:
    """The check will be argued with far more often than the readings change.

    Re-reading the invoice to re-run the check costs a few cents against a $5
    budget, and answers a question nobody asked - the readings are not what is
    in doubt.
    """
    result = runner.invoke(
        app,
        ["confidence", "unused.pdf", "--from-extraction", str(_saved_readings(tmp_path))],
    )
    assert result.exit_code == 0, result.output
    assert "auto_ok: yes" in result.output


def test_replaying_an_ambiguous_date_says_it_is_not_a_failure(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "confidence",
            "unused.pdf",
            "--from-extraction",
            str(_saved_readings(tmp_path, ambiguous=True)),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "invoice_date ambiguous" in result.output
    assert "2024-03-09 or 2024-09-03" in result.output
    assert "not a failure" in result.output


def test_a_received_date_settles_the_replayed_ambiguity(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "confidence",
            "unused.pdf",
            "--from-extraction",
            str(_saved_readings(tmp_path, ambiguous=True)),
            "--received-at",
            "2024-03-12",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "received 2024-03-12 -> 2024-03-09" in result.output
    assert "date_resolved_from_receipt_window" in result.output


def test_the_country_flag_overrides_what_was_saved(tmp_path: Path) -> None:
    """The country is the one input that is not a property of the document."""
    result = runner.invoke(
        app,
        [
            "confidence",
            "unused.pdf",
            "--from-extraction",
            str(_saved_readings(tmp_path, ambiguous=True)),
            "--country",
            "US",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "resolved to 2024-09-03" in result.output


def test_a_file_that_is_not_readings_fails_cleanly(tmp_path: Path) -> None:
    junk = tmp_path / "junk.json"
    junk.write_text('{"nope": 1}', encoding="utf-8")
    result = runner.invoke(app, ["confidence", "unused.pdf", "--from-extraction", str(junk)])
    assert result.exit_code == 1
    assert "not a saved readings file" in result.output


def test_a_missing_readings_file_fails_cleanly(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["confidence", "unused.pdf", "--from-extraction", str(tmp_path / "nope.json")]
    )
    assert result.exit_code == 1
    assert "could not read saved readings" in result.output


@pytest.mark.parametrize(
    ("classification", "rejected", "expected"),
    [
        (
            ExceptionClassification(
                reason_code=ReasonCode.PRICE_OVER_TOLERANCE,
                suggested_resolver=SuggestedResolver.BUYER,
                human_summary="PO line 2 is billed 3.0% above the order price, against a 2% limit.",
                suggested_action=SuggestedAction.REQUEST_PO_AMENDMENT,
            ),
            None,
            [
                (
                    "explanation : lead=price_over_tolerance, resolver=buyer, "
                    "action=request_po_amendment"
                ),
                (
                    "summary     : [AI-generated] PO line 2 is billed 3.0% above the order "
                    "price, against a 2% limit."
                ),
            ],
        ),
        (None, "failed: ClassificationError", ["explanation : none (failed: ClassificationError)"]),
    ],
)
def test_run_shows_what_the_person_at_pending_human_will_read(  # noqa: PLR0913, PLR0917 - three fixtures, three cases
    born_digital_pdf: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    classification: ExceptionClassification | None,
    rejected: str | None,
    expected: list[str],
) -> None:
    """The summary is on the record, not the trail - so a held run must print it."""

    def _held(record: InvoiceRecord, _ctx: object, **_kwargs: object) -> InvoiceRecord:
        return record.model_copy(
            update={
                "state": InvoiceState.PENDING_HUMAN,
                "classification": classification,
                "classification_rejected": rejected,
            }
        )

    monkeypatch.setattr(cli_module, "loop_run", _held)

    result = runner.invoke(
        app, ["run", str(born_digital_pdf), "--audit-dir", str(tmp_path / "audit")]
    )

    assert result.exit_code == 0, result.output
    assert "final state : PENDING_HUMAN" in result.output
    for line in expected:
        assert line in result.output
