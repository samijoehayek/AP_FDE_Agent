"""What the guardrails found wrong with a document on the way in.

Responsible for the shape of a guardrail flag and nothing else. The checks
themselves live in ``tools/ingest_document.py``; the routing decision lives in
the loop.

**A flag never carries document text.** It says which check fired and where -
page, span count - and never what the document said. Flags are
written into the audit trail and may later be shown to a model explaining the
exception, and an instruction planted in a document must not be able to ride
into either on the back of the check that caught it.
"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import StrictModel
from ap_agent.contracts.enums import InputCheck

__all__ = ["InputFlag"]


class InputFlag(StrictModel):
    """One reason this document goes to a person before any model sees it."""

    check: InputCheck
    detail: str = Field(
        min_length=1,
        max_length=200,
        description="Where and how much, in this system's own words. Never document text.",
    )
