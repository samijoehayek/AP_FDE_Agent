"""Read an invoice a second time, from the PDF's own text layer.

Caller: code. The orchestrator calls this; no model can.
Side effects: MODEL_CALL, LOCAL_READ. Spends tokens and reads the file.

This is the second of two readings, and it is deliberately different from the
first in three ways at once: a different **modality** (characters the document
carries, not pixels), a different **model**, and a different **prompt**. Two
readings are only worth having if they can be wrong in different ways - running
the same model twice mostly reproduces the same mistake with more confidence,
which is worse than one reading because it looks like corroboration.

The text layer is also what makes *grounding* possible. A value the model claims
to have read can be checked against the characters actually in the file, which
turns "the model said 1676976" into "1676976 is in the document" - a much
stronger statement, and the one
:mod:`ap_agent.tools.compute_extraction_confidence` is built on.

No OCR. A scan without a text layer gets ``has_text_layer=False`` and no second
read, and the confidence check treats that honestly as ungrounded rather than
pretending a single reading is two.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import anthropic
import pymupdf
from pydantic import Field, ValidationError

from ap_agent.config import get_settings
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.errors import ExtractionError, UnsupportedDocumentError
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput
from ap_agent.tools.extract_invoice_vision import build_client, load_prompt
from ap_agent.tools.ingest_document import MIN_TEXT_CHARS_PER_PAGE, sniff_media_type

if TYPE_CHECKING:
    from anthropic.types.parsed_message import ParsedMessage

    from ap_agent.tools.pymupdf_types import PdfDocument

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.MODEL_CALL, SideEffect.LOCAL_READ)
REQUIRES_IDEMPOTENCY_KEY = False

PROMPT_VERSION: Final = "extract_text_v1"
"""The version this tool shipped with. The floor, not the default.

Which prompt runs comes from ``Settings.text_extraction_prompt_version``; every
older file stays on disk so a past extraction stays explainable under the prompt
that produced it.
"""


def default_prompt_version() -> str:
    """The prompt version configured for this seat, resolved at call time."""
    return get_settings().text_extraction_prompt_version


"""Distinct from the vision prompt, and recorded on every result."""

DEFAULT_MAX_TOKENS: Final = 16000

NO_TEXT_LAYER_REASON: Final = "no_text_layer"


class ExtractInvoiceTextInput(ToolInput):
    """The document to read, and how to read it."""

    path: Path = Field(description="A PDF. Images are accepted and produce no second read.")
    model_id: str | None = Field(
        default=None,
        description="Overrides Settings.text_extraction_model. Recorded on the result either way.",
    )
    prompt_version: str = Field(default_factory=default_prompt_version, min_length=1, max_length=32)
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1024, le=64000)


class ExtractInvoiceTextOutput(ToolOutput):
    """The document's own text, and what a second model made of it."""

    raw_text: str = Field(
        description="Every page's text joined with form feeds, line breaks preserved. Empty when "
        "there is no text layer. Byte-identical to what the second model was sent, because this "
        "is what grounding is checked against."
    )
    pages: list[str] = Field(
        default_factory=list[str],
        description="Per-page text, in page order. A convenience view: each page is trimmed on "
        "its own, so rejoining these does not reconstruct raw_text exactly.",
    )
    has_text_layer: bool
    second_read: InvoiceExtraction | None = Field(
        default=None,
        description="None when there is no text layer. Never a guess: no OCR happens here.",
    )

    model_id: str | None = None
    prompt_version: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    retry_count: int = Field(
        default=0,
        ge=0,
        description="How many times the client retried before this answer. Surfaced on the "
        "audit row: a call that took three attempts cost three attempts, and a "
        "trail that hides that understates both latency and spend.",
    )


ExtractTextOutput = ExtractInvoiceTextOutput
"""Alias. The package convention names outputs after their tool module."""


def read_text_layer(path: Path) -> tuple[list[str], bool]:
    """Return ``(pages, has_text_layer)`` for a document.

    Line breaks are preserved. A text layer read as one run of words loses the
    label-to-value adjacency that is most of what makes an invoice readable, and
    grounding a date like ``09/03/2024`` needs the characters intact.
    """
    if not path.is_file():
        msg = f"not a file: {path}"
        raise ExtractionError(msg)

    data = path.read_bytes()
    if not data:
        msg = f"empty file: {path}"
        raise ExtractionError(msg)

    media_type, filetype = sniff_media_type(data[:16])
    if media_type != "application/pdf":
        # An image has no text layer to read, and this tool does not OCR.
        return [], False

    try:
        document = cast("PdfDocument", pymupdf.open(stream=data, filetype=filetype))
    except Exception as exc:  # pymupdf raises a broad set of parse errors
        msg = f"could not parse {path} as {media_type}"
        raise ExtractionError(msg) from exc

    with document:
        pages: list[str] = [
            str(document[index].get_text("text")) for index in range(document.page_count)
        ]

    substantive = sum(1 for page in pages if len("".join(page.split())) >= MIN_TEXT_CHARS_PER_PAGE)
    return pages, substantive > 0


def _parsed_or_raise(response: ParsedMessage[InvoiceExtraction]) -> InvoiceExtraction:
    """Return the validated extraction, or explain why there isn't one."""
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        msg = f"the model refused to read the text layer (category: {detail})"
        raise ExtractionError(msg)
    if response.stop_reason == "max_tokens":
        msg = "the second reading hit max_tokens; it is truncated and was discarded"
        raise ExtractionError(msg)

    extraction = response.parsed_output
    if extraction is None:
        msg = f"the API returned no parseable second reading (stop_reason={response.stop_reason})"
        raise ExtractionError(msg)
    return extraction


def extract_invoice_text(payload: ExtractInvoiceTextInput) -> ExtractInvoiceTextOutput:
    """Read the document's text layer, and have a second model interpret it.

    Args:
        payload: The document to read and the model to read it with.

    Returns:
        The raw text, the per-page text, and the second reading if there was
        one to make.

    Raises:
        ExtractionError: The file is unreadable, the API call failed, or the
            response did not satisfy the contract.
        UnsupportedDocumentError: The bytes are not a type this pipeline accepts.
    """
    pages, has_text_layer = read_text_layer(payload.path)

    if not has_text_layer:
        return ExtractInvoiceTextOutput(raw_text="", pages=pages, has_text_layer=False)

    # Trimmed here rather than left to the contract. StrictModel strips every
    # string it validates, so an untrimmed join would be sent to the model and a
    # *different* string returned as `raw_text` - and grounding would then be
    # scored against text the second model never read. Same string, both places.
    raw_text = "\f".join(pages).strip()
    settings = get_settings()
    model_id = payload.model_id or settings.text_extraction_model
    system_prompt = load_prompt(payload.prompt_version)

    started = time.perf_counter()
    try:
        response = build_client().messages.parse(
            model=model_id,
            max_tokens=payload.max_tokens,
            system=system_prompt,
            # The document's text is the entire user turn, and it is data. The
            # prompt above is the only thing that instructs, and it says so.
            messages=[{"role": "user", "content": raw_text}],
            output_format=InvoiceExtraction,
            # No `tools`, exactly as in the vision seat.
        )
    except anthropic.APIError as exc:
        msg = f"text extraction API call failed for {payload.path.name}: {exc}"
        raise ExtractionError(msg) from exc
    except ValidationError as exc:
        msg = f"the second reading did not satisfy the InvoiceExtraction contract: {exc}"
        raise ExtractionError(msg) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    return ExtractInvoiceTextOutput(
        raw_text=raw_text,
        pages=pages,
        has_text_layer=True,
        second_read=_parsed_or_raise(response),
        model_id=response.model or model_id,
        prompt_version=payload.prompt_version,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
    )


__all__ = [
    "NO_TEXT_LAYER_REASON",
    "PROMPT_VERSION",
    "ExtractInvoiceTextInput",
    "ExtractInvoiceTextOutput",
    "ExtractTextOutput",
    "UnsupportedDocumentError",
    "default_prompt_version",
    "extract_invoice_text",
    "read_text_layer",
]
