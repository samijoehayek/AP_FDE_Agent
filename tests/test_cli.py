"""The CLI surface: implemented commands work, stubbed ones fail visibly."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ap_agent import __version__
from ap_agent.cli import app
from ap_agent.states.machine import InvoiceState

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


def test_run_is_stubbed_and_says_so() -> None:
    result = runner.invoke(app, ["run", "inv-1"])
    assert result.exit_code == 2
    assert "not implemented" in result.output
