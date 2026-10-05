"""Write a human explanation of an exception the rules already found.

Caller: code. The orchestrator calls this; no model can.
Side effects: MODEL_CALL.

The second and last LLM seat. The exception already exists and its reason
codes are already fixed by ``compute_match``; the model chooses which reason
to lead with, picks a resolver and a next action from closed enums, and
writes at most 120 words of prose for the approver's queue.

It does not decide whether there is an exception, and its output is not read
by any rule. The controls are the same shape as the extraction seat's, plus one:

* **No tools**, and a schema-constrained response (``messages.parse`` into
  :class:`~ap_agent.contracts.exceptions.ExceptionClassification`).
* **No prose in.** The seat is shown codes, numbers and ERP ids - the
  ``MatchResult``, :class:`HeaderNumbers`, :class:`PoSnapshot`,
  :class:`VendorSummary` and the tolerances - and never the document, a line
  description, ``suspicious_text``, the remit-to block or an evidence snippet.
  The extraction seat reads untrusted text because it must; this one has no
  reason to, so the input contract cannot carry any.
* **Checked on the way out.** The lead ``reason_code`` must be one the
  ``MatchResult`` already reported, the summary must be within 120 words, and
  the summary goes through the output filter like any other model text. A
  classification that fails is *rejected*, not raised: the call happened and
  was paid for, so the result says why it was refused and the audit row can
  record it. The exception still reaches a person either way - its reason codes
  do not depend on the prose.

What this does not do: route, fall back, or retry. Those are the loop's.
"""

from __future__ import annotations

import functools
import json
import time
from typing import TYPE_CHECKING, Final, Self

import anthropic
from pydantic import Field, ValidationError, model_validator

from ap_agent.config import REPO_ROOT, get_settings
from ap_agent.contracts.exceptions import (
    ExceptionClassification,
    HeaderNumbers,
    PoSnapshot,
    VendorSummary,
)
from ap_agent.contracts.guardrails import GuardrailConfig
from ap_agent.contracts.matching import MatchResult
from ap_agent.errors import ClassificationError
from ap_agent.guardrails.output_filter import screen_text
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput
from ap_agent.tools.extract_invoice_vision import build_client

if TYPE_CHECKING:
    from pathlib import Path

    from anthropic.types.parsed_message import ParsedMessage

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.MODEL_CALL,)
REQUIRES_IDEMPOTENCY_KEY = False

MAX_SUMMARY_WORDS: Final = 120
"""A summary a reviewer reads in one glance. Checked in code, after the call.

Not a contract validator: a validator would fail inside ``messages.parse`` and
throw away the usage figures with the response, and a call that was paid for
must be recorded as paid for.
"""

DEFAULT_MAX_TOKENS: Final = 2048

PROMPTS_DIR: Final[Path] = REPO_ROOT / "prompts"

SUMMARY_FIELD: Final = "human_summary"


def default_prompt_version() -> str:
    """The configured prompt for this seat, resolved at call time."""
    return get_settings().classification_prompt_version


@functools.lru_cache(maxsize=4)
def load_prompt(version: str) -> str:
    """Return the system prompt for ``version``.

    Raises:
        ClassificationError: No such prompt file. An unversioned prompt cannot
            be audited, so a missing one is a deployment error, not a default.
    """
    path = PROMPTS_DIR / f"{version}.md"
    if not path.is_file():
        msg = f"no prompt file for version {version!r} at {path}"
        raise ClassificationError(msg)
    return path.read_text(encoding="utf-8")


class ClassifyExceptionInput(ToolInput):
    """Input for :func:`classify_exception`. Codes, numbers and ids - no prose.

    There is deliberately no field here that can hold an extraction, a
    document, or any string a vendor wrote. The ``from_...`` constructors on the
    three snapshot contracts are how a caller gets from the full objects to
    these, and they drop the prose on the way.
    """

    match_result: MatchResult
    header_numbers: HeaderNumbers
    po_snapshot: PoSnapshot | None = Field(
        description="None when there was no order to compare - PO not found, for one."
    )
    vendor_summary: VendorSummary | None = Field(
        description="None when the vendor did not resolve."
    )
    config: GuardrailConfig = Field(
        description="The guardrails the verdict was reached under. The model is shown only "
        "the tolerances and the version; the output filter is applied to its summary."
    )
    model_id: str | None = Field(
        default=None,
        max_length=128,
        description="Overrides Settings.classification_model. Recorded on the result either way.",
    )
    prompt_version: str = Field(
        default_factory=default_prompt_version,
        min_length=1,
        max_length=32,
        description="Selects prompts/<version>.md and is recorded on the result.",
    )
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=256, le=8192)


class ClassifyExceptionOutput(ToolOutput):
    """The classification, or why it was refused - and what the call cost either way."""

    classification: ExceptionClassification | None = Field(
        description="None when the checks in code rejected what the model returned."
    )
    rejected: str | None = Field(
        default=None,
        max_length=500,
        description="Why the classification was refused, in this system's words. Names the "
        "check and the field, never the model's text.",
    )
    model_id: str = Field(description="As reported by the API, not as requested.")
    prompt_version: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    retry_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> Self:
        """Accepted or rejected, never both and never neither."""
        if (self.classification is None) == (self.rejected is None):
            msg = "exactly one of classification and rejected must be set"
            raise ValueError(msg)
        return self


def build_user_message(payload: ClassifyExceptionInput) -> str:
    """The one user turn: the structured facts, as JSON.

    Only these keys, and only from the input contract - which is what makes
    "no prose in" checkable. The config is reduced to its version and its
    tolerances; the approval matrix and the filter patterns are not the
    model's business.
    """
    facts = {
        "match_result": payload.match_result.model_dump(mode="json"),
        "header_numbers": payload.header_numbers.model_dump(mode="json"),
        "po_snapshot": (
            payload.po_snapshot.model_dump(mode="json") if payload.po_snapshot else None
        ),
        "vendor_summary": (
            payload.vendor_summary.model_dump(mode="json") if payload.vendor_summary else None
        ),
        "tolerances": payload.config.tolerances.model_dump(mode="json"),
        "config_version": payload.config.config_version,
    }
    return json.dumps(facts, indent=2, sort_keys=True)


def vet(
    classification: ExceptionClassification, match_result: MatchResult, config: GuardrailConfig
) -> str | None:
    """Return why this classification must be refused, or None if it may stand.

    Three checks, in the order a reviewer would care about them:

    1. **The lead reason is one the rules reported.** The model chooses which
       code to lead with; it may not introduce one. A code the ``MatchResult``
       does not carry is an invented reason, whatever the prose says.
    2. **The summary is within 120 words.**
    3. **The summary passes the output filter.** It is model-written text that
       a person will read and may act on, so the same patterns apply to it as to
       a reading - an account number, a link, an instruction to pay somewhere.

    The reasons name the check and the field, never the model's text.
    """
    if classification.reason_code not in match_result.reason_codes:
        reported = "|".join(code.value for code in match_result.reason_codes)
        return (
            f"reason_code={classification.reason_code.value} is not one the match "
            f"reported ({reported})"
        )

    words = len(classification.human_summary.split())
    if words > MAX_SUMMARY_WORDS:
        return f"{SUMMARY_FIELD} is {words} words; the limit is {MAX_SUMMARY_WORDS}"

    flags = screen_text(SUMMARY_FIELD, classification.human_summary, config.output_filter)
    if flags:
        return "output_filter: " + "|".join(f"{flag.check}({flag.field})" for flag in flags)
    return None


def _parsed_or_raise(response: ParsedMessage[ExceptionClassification]) -> ExceptionClassification:
    """Return the parsed classification, or say why there is none."""
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        msg = f"the model refused to classify the exception (category: {detail})"
        raise ClassificationError(msg)
    if response.stop_reason == "max_tokens":
        msg = "the response hit max_tokens; the classification is truncated and was discarded"
        raise ClassificationError(msg)
    classification = response.parsed_output
    if classification is None:
        msg = f"the API returned no parseable classification (stop_reason={response.stop_reason})"
        raise ClassificationError(msg)
    return classification


def classify_exception(payload: ClassifyExceptionInput) -> ClassifyExceptionOutput:
    """Explain a held invoice for the person who has to resolve it.

    Args:
        payload: The verdict and the numbers behind it. No document text.

    Returns:
        The classification, or the reason it was refused, with the model, the
        prompt version, the tokens and the latency the audit row needs.

    Raises:
        ClassificationError: The match carries no reason codes (there is no
            exception to explain, and nothing is spent finding that out), or the
            API call failed, refused, truncated, or returned nothing parseable.
    """
    if not payload.match_result.reason_codes:
        msg = "the match carries no reason codes; there is no exception to classify"
        raise ClassificationError(msg)

    model_id = payload.model_id or get_settings().classification_model
    system_prompt = load_prompt(payload.prompt_version)

    started = time.perf_counter()
    try:
        response = build_client().messages.parse(
            model=model_id,
            max_tokens=payload.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": build_user_message(payload)}],
            output_format=ExceptionClassification,
            # No `tools`, and no sampling parameters - for the reasons given on
            # the extraction seat. The response surface is one classification.
        )
    except anthropic.APIError as exc:
        msg = f"classification API call failed: {exc}"
        raise ClassificationError(msg) from exc
    except ValidationError as exc:
        msg = f"the model's response did not satisfy ExceptionClassification: {exc}"
        raise ClassificationError(msg) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    classification = _parsed_or_raise(response)
    rejected = vet(classification, payload.match_result, payload.config)
    return ClassifyExceptionOutput(
        classification=None if rejected else classification,
        rejected=rejected,
        model_id=response.model or model_id,
        prompt_version=payload.prompt_version,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
    )
