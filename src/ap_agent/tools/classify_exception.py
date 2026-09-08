"""Write a human explanation of an exception the rules already found.

Caller: code. The orchestrator calls this; no model can.
Side effects: MODEL_CALL.

The second and last LLM seat. The exception already exists and its reason
codes are already fixed by ``compute_match``; the model chooses which reason
to lead with, picks a resolver and a next action from closed enums, and
writes at most 700 characters of prose for the approver's queue.

It does not decide whether there is an exception, and its output is not read
by any rule. If this call fails, the exception still routes - just with a
generated summary instead of a written one.

Not implemented: the prompt, the reason-code cross-check against the
MatchResult, and the fallback summary.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.exceptions import ExceptionClassification
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.matching import MatchResult
from ap_agent.contracts.vendor import VendorRef
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.MODEL_CALL,)
REQUIRES_IDEMPOTENCY_KEY = False


class ClassifyExceptionInput(ToolInput):
    """Input for :func:`classify_exception`."""

    match_result: MatchResult
    extraction: InvoiceExtraction
    vendor: VendorRef | None = None
    model_id: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=32)


class ClassifyExceptionOutput(ToolOutput):
    """Output of :func:`classify_exception`."""

    classification: ExceptionClassification
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


def classify_exception(payload: ClassifyExceptionInput) -> ClassifyExceptionOutput:
    """Write a human explanation of an exception the rules already found.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
