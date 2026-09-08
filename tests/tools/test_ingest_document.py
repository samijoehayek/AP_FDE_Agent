"""ingest_document against real files, not mocks.

The whole value of this tool is that it copes with what actually arrives, so the
fixtures build genuine PDFs and genuine blurred images.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ap_agent.errors import IngestionError, UnsupportedDocumentError
from ap_agent.tools.ingest_document import (
    MIN_TEXT_CHARS_PER_PAGE,
    IngestDocumentInput,
    ingest_document,
)

# --- identity ---------------------------------------------------------------


def test_sha256_is_the_hash_of_the_raw_bytes(born_digital_pdf: Path) -> None:
    """The document's identity is its bytes, computed before anything parses them."""
    result = ingest_document(IngestDocumentInput(path=born_digital_pdf))
    assert result.sha256 == hashlib.sha256(born_digital_pdf.read_bytes()).hexdigest()
    assert result.byte_size == born_digital_pdf.stat().st_size


def test_the_same_file_hashes_the_same_twice(born_digital_pdf: Path) -> None:
    first = ingest_document(IngestDocumentInput(path=born_digital_pdf, compute_sharpness=False))
    second = ingest_document(IngestDocumentInput(path=born_digital_pdf, compute_sharpness=False))
    assert first.sha256 == second.sha256


# --- structure --------------------------------------------------------------


def test_a_two_page_pdf_reports_two_pages(born_digital_pdf: Path) -> None:
    result = ingest_document(IngestDocumentInput(path=born_digital_pdf))
    assert result.page_count == 2
    assert result.media_type == "application/pdf"


def test_a_born_digital_pdf_has_a_text_layer(born_digital_pdf: Path) -> None:
    result = ingest_document(IngestDocumentInput(path=born_digital_pdf))
    assert result.has_text_layer
    assert result.pages_with_text == 2
    assert result.is_born_digital
    assert result.text_char_count > MIN_TEXT_CHARS_PER_PAGE


def test_an_image_only_pdf_has_no_text_layer(scanned_pdf: Path) -> None:
    """This is the flag that routes an invoice to the expensive vision path."""
    result = ingest_document(IngestDocumentInput(path=scanned_pdf))
    assert not result.has_text_layer
    assert result.pages_with_text == 0
    assert not result.is_born_digital


def test_a_png_is_a_single_page_document(sharp_png: Path) -> None:
    result = ingest_document(IngestDocumentInput(path=sharp_png))
    assert result.media_type == "image/png"
    assert result.page_count == 1
    assert not result.has_text_layer


# --- sharpness --------------------------------------------------------------


def test_a_blurred_page_scores_far_lower_than_a_sharp_one(
    sharp_png: Path, blurred_png: Path
) -> None:
    """The triage signal: a low score routes to a human before a model reads numbers."""
    sharp = ingest_document(IngestDocumentInput(path=sharp_png))
    blurred = ingest_document(IngestDocumentInput(path=blurred_png))

    assert sharp.min_sharpness is not None
    assert blurred.min_sharpness is not None
    assert blurred.min_sharpness < sharp.min_sharpness / 10


def test_one_score_per_page(born_digital_pdf: Path) -> None:
    result = ingest_document(IngestDocumentInput(path=born_digital_pdf))
    assert len(result.page_sharpness) == result.page_count
    assert all(score >= 0 for score in result.page_sharpness)


def test_scoring_can_be_skipped(born_digital_pdf: Path) -> None:
    result = ingest_document(IngestDocumentInput(path=born_digital_pdf, compute_sharpness=False))
    assert result.page_sharpness == []
    assert result.min_sharpness is None


def test_scoring_is_capped_at_max_pages_scored(born_digital_pdf: Path) -> None:
    result = ingest_document(IngestDocumentInput(path=born_digital_pdf, max_pages_scored=1))
    assert len(result.page_sharpness) == 1
    assert result.page_count == 2


# --- failure modes ----------------------------------------------------------


def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="not a file"):
        ingest_document(IngestDocumentInput(path=tmp_path / "nope.pdf"))


def test_an_empty_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    path.touch()
    with pytest.raises(IngestionError):
        ingest_document(IngestDocumentInput(path=path))


def test_media_type_comes_from_the_bytes_not_the_extension(tmp_path: Path) -> None:
    """An attacker controls the filename; the first eight bytes are harder to fake."""
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"MZ\x90\x00this is a windows executable, not a pdf")
    with pytest.raises(UnsupportedDocumentError, match="unrecognised document header"):
        ingest_document(IngestDocumentInput(path=path))


def test_a_truncated_pdf_raises_ingestion_error(tmp_path: Path) -> None:
    path = tmp_path / "truncated.pdf"
    path.write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog")
    with pytest.raises(IngestionError):
        ingest_document(IngestDocumentInput(path=path))
