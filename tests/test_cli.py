"""The CLI surface: implemented commands work, stubbed ones fail visibly."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ap_agent import __version__
from ap_agent.cli import app
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.loop import runner as runner_module
from ap_agent.states.machine import InvoiceState
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
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"not a document at all")
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
    """The extraction seat is replaced, so this never reaches the API."""
    extraction = InvoiceExtraction.model_validate(
        {
            "vendor_name": "Acme",
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

    monkeypatch.setattr(runner_module, "extract_invoice_vision", _extract)
    monkeypatch.setattr(runner_module.RunContext, "__init__", runner_module.RunContext.__init__)
    result = runner.invoke(
        app,
        ["run", str(born_digital_pdf), "--audit-dir", str(tmp_path / "audit")],
    )
    assert result.exit_code == 0, result.output
    assert "final state : CLOSED" in result.output
    assert "steps       : 14" in result.output
    assert "chain intact: True" in result.output
