"""Read an invoice from page images with a vision model.

Caller: code. The orchestrator calls this; no model can.
Side effects: MODEL_CALL. Spends tokens. No writes.

This is one of the two seats an LLM occupies in this system, and the one that
touches untrusted input. The rules it operates under:

* The model is given **no tools**. It cannot look anything up, cannot call the
  ERP, and cannot cause an effect. Its entire output surface is one
  ``InvoiceExtraction``.
* Document content is data. It is delivered inside a clearly-delimited block and
  the system prompt states that instructions found there are to be reported in
  ``suspicious_text``, never followed.
* Remittance and bank details are not requested and have nowhere to go in the
  output schema. See ``ap_agent.contracts.invoice``.

Not implemented in this scaffold: the prompt, the block delimiters, the
retry-on-validation-failure loop, and the page-batching strategy.
"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import Confidence
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.MODEL_CALL,)
REQUIRES_IDEMPOTENCY_KEY = False


class ExtractInvoiceVisionInput(ToolInput):
    """Page images and the model to read them with."""

    document_sha256: str = Field(min_length=64, max_length=64)
    page_image_paths: list[str] = Field(
        min_length=1,
        max_length=20,
        description="Rendered page images, in order. Bounded: a 400-page attachment is an "
        "exception for a human, not a large prompt.",
    )
    model_id: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=32)


class ExtractInvoiceVisionOutput(ToolOutput):
    """The extraction, plus what it cost to get it."""

    extraction: InvoiceExtraction
    model_self_reported_confidence: Confidence | None = Field(
        default=None,
        description="What the model says about its own reading. Advisory only - the calibrated "
        "score comes from compute_extraction_confidence, which the model cannot see.",
    )
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


def extract_invoice_vision(payload: ExtractInvoiceVisionInput) -> ExtractInvoiceVisionOutput:
    """Extract invoice fields from page images.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
