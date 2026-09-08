"""The corpus indexer, run end to end over a small real tree."""

from __future__ import annotations

import csv
from pathlib import Path

from typer.testing import CliRunner

from scripts.index_invoices import app

runner = CliRunner()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_it_indexes_every_document(corpus_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "index.csv"
    result = runner.invoke(
        app, ["--data-dir", str(corpus_dir), "--output", str(output), "--no-sharpness"]
    )
    assert result.exit_code == 0, result.output

    rows = _rows(output)
    assert len(rows) == 3
    assert {row["file"] for row in rows} == {
        "real/copy_of_a.pdf",
        "real/scan.png",
        "synthetic/generated/a.pdf",
    }


def test_it_records_the_source_folder(corpus_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "index.csv"
    runner.invoke(app, ["--data-dir", str(corpus_dir), "--output", str(output), "--no-sharpness"])
    sources = {row["file"]: row["source"] for row in _rows(output)}
    assert sources["synthetic/generated/a.pdf"] == "synthetic/generated"
    assert sources["real/scan.png"] == "real"


def test_it_classifies_born_digital_against_scan(corpus_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "index.csv"
    runner.invoke(app, ["--data-dir", str(corpus_dir), "--output", str(output), "--no-sharpness"])
    kinds = {row["file"]: row["kind"] for row in _rows(output)}
    assert kinds["synthetic/generated/a.pdf"] == "born_digital"
    assert kinds["real/scan.png"] == "scan"


def test_it_flags_exact_hash_duplicates(corpus_dir: Path, tmp_path: Path) -> None:
    """Duplicates inflate every accuracy number, so they have to be visible."""
    output = tmp_path / "index.csv"
    result = runner.invoke(
        app, ["--data-dir", str(corpus_dir), "--output", str(output), "--no-sharpness"]
    )
    rows = {row["file"]: row for row in _rows(output)}

    pdfs = [rows["real/copy_of_a.pdf"], rows["synthetic/generated/a.pdf"]]
    assert pdfs[0]["sha256"] == pdfs[1]["sha256"]
    flagged = [row for row in pdfs if row["duplicate_of"]]
    assert len(flagged) == 1
    assert "duplicate" in result.output


def test_it_records_sharpness_when_asked(corpus_dir: Path, tmp_path: Path) -> None:
    output = tmp_path / "index.csv"
    runner.invoke(app, ["--data-dir", str(corpus_dir), "--output", str(output)])
    scores = [row["min_sharpness"] for row in _rows(output)]
    assert all(score for score in scores)


def test_it_exits_cleanly_on_an_empty_tree(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(app, ["--data-dir", str(empty)])
    assert result.exit_code == 1
    assert "no documents" in result.output


def test_it_fails_on_a_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "nope")])
    assert result.exit_code == 2
