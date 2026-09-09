"""Read an invoice document with a Claude model.

Caller: code. The orchestrator calls this; no model can.
Side effects: MODEL_CALL, LOCAL_READ. Spends tokens and reads the file. No writes.

This is one of the two seats an LLM occupies in this system, and the only one
that touches untrusted input. The controls that make that safe are structural,
not persuasive:

* **The model has no tools.** No ``tools`` argument is passed, so there is
  nothing for injected text to reach for. A document can say anything it likes;
  the model's entire output surface is one ``InvoiceExtraction``.
* **The response is schema-constrained.** Structured outputs
  (``output_config.format``, via the SDK's ``messages.parse`` helper) mean the
  API itself will not emit a shape the contract does not allow. Free-form prose
  is not a possible response, so the classic "ignore your instructions and
  reply with X" attack has nowhere to land.
* **The schema has no field for a payment destination.** See
  ``ap_agent.contracts.invoice``.

The document is sent as a single content block - a ``document`` block for PDFs,
an ``image`` block for images - alongside a system prompt that names the
document as data. Nothing from the file is ever interpolated into the prompt.
"""

from __future__ import annotations

import base64
import functools
import io
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

import anthropic
from anthropic.types import (
    Base64ImageSourceParam,
    Base64PDFSourceParam,
    DocumentBlockParam,
    ImageBlockParam,
)
from pydantic import Field, ValidationError

from ap_agent.config import REPO_ROOT, get_settings
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.errors import ExtractionError
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput
from ap_agent.tools.ingest_document import sniff_media_type

if TYPE_CHECKING:
    from anthropic.types.parsed_message import ParsedMessage

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.MODEL_CALL, SideEffect.LOCAL_READ)
REQUIRES_IDEMPOTENCY_KEY = False

PROMPT_VERSION: Final = "extract_v1"
"""Recorded on every result and every AuditEvent.

A prompt change means a new file and a new version, never an edit in place - an
extraction from last month has to stay explainable under the prompt that
produced it.
"""

PROMPTS_DIR: Final[Path] = REPO_ROOT / "prompts"

DEFAULT_MAX_TOKENS: Final = 16000

_API_IMAGE_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
"""Image media types the Messages API accepts in an ``image`` block.

TIFF is absent, which is why :func:`_transcode_to_png` exists: scanned invoices
arrive as TIFF often enough that rejecting them would be a gap, and re-encoding
is lossless for this purpose.
"""


@functools.lru_cache(maxsize=4)
def load_prompt(version: str) -> str:
    """Return the system prompt text for ``version``.

    Args:
        version: A prompt version, e.g. ``"extract_v1"``.

    Returns:
        The prompt file's contents.

    Raises:
        ExtractionError: The prompt file does not exist. A missing prompt is a
            deployment error, not something to paper over with a default - an
            unversioned prompt cannot be audited.
    """
    path = PROMPTS_DIR / f"{version}.md"
    if not path.is_file():
        msg = f"no prompt file for version {version!r} at {path}"
        raise ExtractionError(msg)
    return path.read_text(encoding="utf-8")


def _transcode_to_png(data: bytes) -> bytes:
    """Re-encode an image the API will not accept into PNG."""
    from PIL import Image  # noqa: PLC0415 - only needed on the TIFF path

    try:
        with Image.open(io.BytesIO(data)) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="PNG")
    except OSError as exc:
        msg = "could not re-encode the document as PNG"
        raise ExtractionError(msg) from exc
    return buffer.getvalue()


def build_content_block(path: Path) -> ImageBlockParam | DocumentBlockParam:
    """Return the API content block for ``path``.

    PDFs go as a ``document`` block so the model sees pages rather than a
    rendering; images go as an ``image`` block.

    Args:
        path: The document to send.

    Returns:
        A single content block, ready to place in a user message.

    Raises:
        ExtractionError: The path is not a readable file.
        UnsupportedDocumentError: The bytes are not a type this pipeline accepts.
    """
    if not path.is_file():
        msg = f"not a file: {path}"
        raise ExtractionError(msg)

    data = path.read_bytes()
    if not data:
        msg = f"empty file: {path}"
        raise ExtractionError(msg)

    media_type, _filetype = sniff_media_type(data[:16])

    if media_type == "application/pdf":
        return DocumentBlockParam(
            type="document",
            source=Base64PDFSourceParam(
                type="base64",
                media_type="application/pdf",
                data=base64.standard_b64encode(data).decode("ascii"),
            ),
        )

    if media_type not in _API_IMAGE_MEDIA_TYPES:
        data = _transcode_to_png(data)
        media_type = "image/png"

    return ImageBlockParam(
        type="image",
        source=Base64ImageSourceParam(
            type="base64",
            # Narrowed by the _API_IMAGE_MEDIA_TYPES check above; the SDK types
            # this as a Literal union that a runtime string cannot satisfy.
            media_type=cast(
                'Literal["image/jpeg", "image/png", "image/gif", "image/webp"]', media_type
            ),
            data=base64.standard_b64encode(data).decode("ascii"),
        ),
    )


def _build_client() -> anthropic.Anthropic:
    """Construct the API client.

    An empty ``ANTHROPIC_API_KEY`` is not the same as no credentials - the SDK
    also resolves ``ANTHROPIC_AUTH_TOKEN`` and an ``ant auth login`` profile - so
    an empty setting means "let the SDK decide", not "send an empty key".
    """
    key = get_settings().anthropic_api_key.get_secret_value()
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


class ExtractInvoiceVisionInput(ToolInput):
    """The document to read, and how to read it."""

    path: Path = Field(description="PDF, PNG, JPEG, GIF, WEBP or TIFF. TIFF is re-encoded.")
    model_id: str | None = Field(
        default=None,
        description="Overrides Settings.extraction_model. Recorded on the result either way.",
    )
    prompt_version: str = Field(
        default=PROMPT_VERSION,
        min_length=1,
        max_length=32,
        description="Selects prompts/<version>.md and is recorded on the result.",
    )
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1024, le=64000)


class ExtractInvoiceVisionOutput(ToolOutput):
    """The extraction, plus what it cost to get it."""

    extraction: InvoiceExtraction
    model_id: str = Field(description="As reported by the API, not as requested.")
    prompt_version: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


def _parsed_or_raise(response: ParsedMessage[InvoiceExtraction]) -> InvoiceExtraction:
    """Return the validated extraction, or explain why there isn't one."""
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        msg = f"the model refused to read the document (category: {detail})"
        raise ExtractionError(msg)
    if response.stop_reason == "max_tokens":
        msg = "the response hit max_tokens; the extraction is truncated and was discarded"
        raise ExtractionError(msg)

    extraction = response.parsed_output
    if extraction is None:
        msg = f"the API returned no parseable extraction (stop_reason={response.stop_reason})"
        raise ExtractionError(msg)
    return extraction


def extract_invoice_vision(payload: ExtractInvoiceVisionInput) -> ExtractInvoiceVisionOutput:
    """Extract invoice fields from a document.

    Args:
        payload: The document to read and the model to read it with.

    Returns:
        The extraction alongside the model, prompt version, token counts and
        latency the audit trail needs.

    Raises:
        ExtractionError: The API call failed, the model refused, the response
            was truncated, or the result did not validate against the contract.
        UnsupportedDocumentError: The file is not a type this pipeline accepts.
    """
    settings = get_settings()
    model_id = payload.model_id or settings.extraction_model
    block = build_content_block(payload.path)
    system_prompt = load_prompt(payload.prompt_version)

    started = time.perf_counter()
    try:
        response = _build_client().messages.parse(
            model=model_id,
            max_tokens=payload.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": [block]}],
            output_format=InvoiceExtraction,
            # No `tools`: the model that reads documents has none, so there is
            # nothing for injected text to reach for.
            #
            # No `temperature` either. The parameter is rejected outright by the
            # models this seat runs on, and messages.parse() does not accept it -
            # determinism here comes from the schema constraint, which is a
            # stronger guarantee than a sampling setting ever was.
        )
    except anthropic.APIError as exc:
        msg = f"extraction API call failed for {payload.path.name}: {exc}"
        raise ExtractionError(msg) from exc
    except ValidationError as exc:
        msg = f"the model's response did not satisfy the InvoiceExtraction contract: {exc}"
        raise ExtractionError(msg) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    return ExtractInvoiceVisionOutput(
        extraction=_parsed_or_raise(response),
        model_id=response.model or model_id,
        prompt_version=payload.prompt_version,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
    )
