"""Turn a file on disk into the facts the pipeline needs before reading it.

Caller: code. The orchestrator calls this; no model can.
Side effects: LOCAL_READ. Reads the file. Writes nothing.

This is the first thing that touches a document and the only tool in this
package that is fully implemented in the scaffold, because everything after it
depends on its output being trustworthy:

* ``sha256`` is the document's identity. It is the strongest duplicate key
  available and it is what the audit trail refers to, so it is computed over the
  raw bytes before anything else parses, normalises, or re-encodes them.
* ``has_text_layer`` decides whether extraction goes down the cheap text path or
  the vision path. Getting it wrong costs either accuracy or money.
* ``page_sharpness`` is the Laplacian variance of each rendered page. A blurred
  or skewed scan produces a low score, and a low score routes the invoice to a
  human *before* a model is asked to read numbers off it. It is a triage signal,
  not a physical measurement: compare it against the thresholds in the
  guardrails config, not against an absolute.

Nothing here interprets content. A document is bytes, a page count, and a
quality score until an extraction contract says otherwise.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import numpy as np
import pymupdf
from pydantic import Field

from ap_agent.errors import IngestionError, UnsupportedDocumentError
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from ap_agent.tools.pymupdf_types import PdfDocument, PdfPage

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.LOCAL_READ,)
REQUIRES_IDEMPOTENCY_KEY = False

_MAGIC: Final[tuple[tuple[bytes, str, str], ...]] = (
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpeg"),
    (b"II*\x00", "image/tiff", "tiff"),
    (b"MM\x00*", "image/tiff", "tiff"),
)
"""Media type is sniffed from magic bytes, never from the filename.

An attacker controls the filename; they do not control the first eight bytes as
easily, and a ``.pdf`` that is really something else should fail here rather
than three functions later.
"""

MIN_TEXT_CHARS_PER_PAGE: Final = 20
"""Below this, a page's "text layer" is scanner noise rather than content."""

SHARPNESS_TARGET_PX: Final = 1000
"""Long edge of the render used for the sharpness score.

Fixed so the score is comparable across documents: Laplacian variance scales
with resolution, so measuring a 300-dpi scan and a 72-dpi one at their native
sizes would compare nothing.
"""

_CHUNK_BYTES: Final = 1 << 20

_LAPLACIAN_KERNEL_SPAN: Final = 3
"""The 4-neighbour kernel needs a 3x3 neighbourhood to have an interior."""


class IngestDocumentInput(ToolInput):
    """Input for :func:`ingest_document`."""

    path: Path = Field(description="Absolute or CWD-relative path to the source document.")
    compute_sharpness: bool = Field(
        default=True,
        description="Rendering pages costs time. Turn it off for bulk indexing where only the "
        "hash and page count are needed.",
    )
    max_pages_scored: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Sharpness is scored on the first N pages. A 60-page attachment does not "
        "need 60 renders to be judged legible.",
    )


class IngestDocumentOutput(ToolOutput):
    """Everything known about a document before anyone reads it."""

    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=64)
    page_count: int = Field(ge=1)

    has_text_layer: bool = Field(
        description="True when at least one page carries enough embedded text to extract from."
    )
    text_char_count: int = Field(
        ge=0, description="Non-whitespace characters in the embedded text layer, across all pages."
    )
    pages_with_text: int = Field(ge=0)

    page_sharpness: list[float] = Field(
        default_factory=list[float],
        description="Laplacian variance per scored page, in page order. Empty when scoring was "
        "skipped. Relative: compare against the guardrails thresholds.",
    )

    @property
    def is_born_digital(self) -> bool:
        """True when every page carries a text layer.

        Born-digital invoices go down the text extraction path. A PDF where only
        some pages have text is a scan with an OCR layer bolted on, or a merged
        document - either way it is not born digital and is treated as a scan.
        """
        return self.has_text_layer and self.pages_with_text == self.page_count

    @property
    def min_sharpness(self) -> float | None:
        """Worst scored page, or None when scoring was skipped."""
        return min(self.page_sharpness) if self.page_sharpness else None


def _sha256_file(path: Path) -> tuple[str, int]:
    """Hash a file in chunks and return ``(hex_digest, byte_size)``."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def sniff_media_type(header: bytes) -> tuple[str, str]:
    """Return ``(media_type, pymupdf_filetype)`` from a file's leading bytes.

    Raises:
        UnsupportedDocumentError: The bytes match nothing this pipeline accepts.
    """
    for magic, media_type, filetype in _MAGIC:
        if header.startswith(magic):
            return media_type, filetype
    msg = f"unrecognised document header: {header[:8]!r}"
    raise UnsupportedDocumentError(msg)


def _laplacian_variance(gray: NDArray[np.float32]) -> float:
    """Return the variance of the 4-neighbour Laplacian of a greyscale image.

    The standard focus measure: convolve with the discrete Laplacian and take
    the variance of the response. A sharp page has strong, varied edge response;
    a blurred one does not. Computed on the interior so no border padding
    convention biases the result.
    """
    if min(gray.shape[0], gray.shape[1]) < _LAPLACIAN_KERNEL_SPAN:
        return 0.0
    laplacian = (
        gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4.0 * gray[1:-1, 1:-1]
    )
    return float(laplacian.var())


def _page_gray(page: PdfPage) -> NDArray[np.float32]:
    """Render one page to a greyscale array normalised to a fixed long edge."""
    rect = page.rect
    longest = max(rect.width, rect.height) or 1.0
    zoom = SHARPNESS_TARGET_PX / longest
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)

    # PyMuPDF pads each row to `stride` bytes; slicing to width drops the padding.
    flat = np.frombuffer(pixmap.samples, dtype=np.uint8)
    rows = flat.reshape(pixmap.height, pixmap.stride)
    return rows[:, : pixmap.width].astype(np.float32)


def ingest_document(payload: IngestDocumentInput) -> IngestDocumentOutput:
    """Hash, identify, and quality-check a document.

    Args:
        payload: The path to read and how much work to do on it.

    Returns:
        The document's identity, structure, and per-page legibility.

    Raises:
        IngestionError: The path does not exist, is not a file, is empty, or
            could not be parsed as the type its bytes claim.
        UnsupportedDocumentError: The media type is not one this pipeline accepts.
    """
    path = payload.path
    if not path.is_file():
        msg = f"not a file: {path}"
        raise IngestionError(msg)

    sha256, byte_size = _sha256_file(path)
    if byte_size == 0:
        msg = f"empty file: {path}"
        raise IngestionError(msg)

    with path.open("rb") as handle:
        header = handle.read(16)
    media_type, filetype = sniff_media_type(header)

    data = path.read_bytes()
    try:
        document = cast("PdfDocument", pymupdf.open(stream=data, filetype=filetype))
    except Exception as exc:  # pymupdf raises a broad set of parse errors
        msg = f"could not parse {path} as {media_type}"
        raise IngestionError(msg) from exc

    with document:
        page_count = document.page_count
        if page_count < 1:
            msg = f"document has no pages: {path}"
            raise IngestionError(msg)

        text_char_count = 0
        pages_with_text = 0
        sharpness: list[float] = []

        for index in range(page_count):
            page = document[index]
            stripped = "".join(page.get_text("text").split())
            text_char_count += len(stripped)
            if len(stripped) >= MIN_TEXT_CHARS_PER_PAGE:
                pages_with_text += 1

            if payload.compute_sharpness and index < payload.max_pages_scored:
                sharpness.append(_laplacian_variance(_page_gray(page)))

    return IngestDocumentOutput(
        sha256=sha256,
        byte_size=byte_size,
        media_type=media_type,
        page_count=page_count,
        has_text_layer=pages_with_text > 0,
        text_char_count=text_char_count,
        pages_with_text=pages_with_text,
        page_sharpness=sharpness,
    )
