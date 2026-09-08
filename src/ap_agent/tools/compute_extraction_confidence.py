"""Score an extraction's trustworthiness without asking the model.

Caller: code. The orchestrator calls this; no model can.
Side effects: NONE.

A model's own confidence is not evidence. This computes a calibrated score
from signals the model does not control: page sharpness from ingestion,
whether the arithmetic self-check passed, whether evidence snippets actually
appear in the document text, field coverage, and agreement between the text
and vision paths when both were run.

The score decides whether an invoice needs a human before it reaches the
matching rules, so it must never be something the document can talk its way
past.

Not implemented: the signal weights and the calibration against the golden set.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import Confidence
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.NONE,)
REQUIRES_IDEMPOTENCY_KEY = False


class ComputeExtractionConfidenceInput(ToolInput):
    """Input for :func:`compute_extraction_confidence`."""

    extraction: InvoiceExtraction
    document_text: str | None = Field(
        default=None, description="Used to verify that evidence snippets are real."
    )
    page_sharpness: list[float] = Field(
        default_factory=list[float], description="Per-page Laplacian variance from ingest_document."
    )
    has_text_layer: bool = True
    second_pass_extraction: InvoiceExtraction | None = Field(
        default=None, description="The other extraction path's result, when both were run."
    )


class ComputeExtractionConfidenceOutput(ToolOutput):
    """Output of :func:`compute_extraction_confidence`."""

    confidence: Confidence
    needs_human_review: bool = Field(
        description="True when the score is below the threshold in the guardrails config."
    )
    signals: dict[str, float] = Field(
        default_factory=dict[str, float],
        description="Per-signal contributions, for the audit trail.",
    )
    unverified_evidence_fields: list[str] = Field(
        default_factory=list[str],
        description="Fields whose evidence snippet was not found in the document text. A "
        "fabricated citation is a strong signal, not a rounding error.",
    )


def compute_extraction_confidence(
    payload: ComputeExtractionConfidenceInput,
) -> ComputeExtractionConfidenceOutput:
    """Score an extraction's trustworthiness without asking the model.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
