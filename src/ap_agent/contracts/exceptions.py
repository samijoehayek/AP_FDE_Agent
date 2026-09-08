"""The second - and last - seat the LLM occupies: explaining an exception.

Given a ``MatchResult`` that deterministic code has already produced, a model
writes a short human summary and picks a resolver and a next action from closed
lists. It does not decide whether there is an exception, what the reason code
is, or whether anything gets paid. It makes a queue item legible to the person
who has to act on it.

``human_summary`` is prose written for an approver. It is display-only: no code
parses it, and no decision reads it.
"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import MAX_SUMMARY_CHARS, StrictModel
from ap_agent.contracts.enums import ReasonCode, SuggestedAction, SuggestedResolver


class ExceptionClassification(StrictModel):
    """A model-written explanation of a deterministically-detected exception."""

    reason_code: ReasonCode = Field(
        description="Must be one the MatchResult already reported. The model is choosing which "
        "reason to lead with, not inventing one.",
    )
    suggested_resolver: SuggestedResolver
    human_summary: str = Field(
        min_length=1,
        max_length=MAX_SUMMARY_CHARS,
        description="Plain-language explanation for the approver's queue. Display only.",
    )
    suggested_action: SuggestedAction = Field(
        description="A routing hint for a human. Never dispatched to a tool.",
    )
