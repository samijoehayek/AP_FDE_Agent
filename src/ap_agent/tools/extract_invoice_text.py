"""Read an invoice from an embedded text layer with a text model.

Caller: code. The orchestrator calls this; no model can.
Side effects: MODEL_CALL.

Preferred over the vision path whenever ``ingest_document`` reported a
text layer: it is cheaper, faster, and reads exact characters rather than
inferring them from pixels. The same rules apply - no tools, document text
is data, instruction-like text is reported rather than obeyed.

Not implemented: the prompt, the text-block delimiting, the layout-preserving
extraction (PyMuPDF "blocks" vs "text"), and the fallback to the vision path
when the text layer turns out to be a scanner artefact.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.MODEL_CALL,)
REQUIRES_IDEMPOTENCY_KEY = False


class ExtractInvoiceTextInput(ToolInput):
    """Input for :func:`extract_invoice_text`."""

    document_sha256: str = Field(min_length=64, max_length=64)
    document_text: str = Field(
        min_length=1,
        max_length=400_000,
        description="Extracted text layer. Untrusted; delivered to the model as data.",
    )
    model_id: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=32)


class ExtractInvoiceTextOutput(ToolOutput):
    """Output of :func:`extract_invoice_text`."""

    extraction: InvoiceExtraction
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


def extract_invoice_text(payload: ExtractInvoiceTextInput) -> ExtractInvoiceTextOutput:
    """Read an invoice from an embedded text layer with a text model.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
