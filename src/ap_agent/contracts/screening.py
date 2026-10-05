"""What the guardrails found wrong with a document, on the way in and the way out.

Responsible for the shape of a guardrail flag and nothing else. The checks
themselves live in ``tools/ingest_document.py`` (input) and
``guardrails/output_filter.py`` (output); the routing decision lives in the loop.

**A flag never carries document text.** It says which check fired and where -
page, span count, field name - and never what the document said. Flags are
written into the audit trail and may later be shown to a model explaining the
exception, and an instruction planted in a document must not be able to ride
into either on the back of the check that caught it.
"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import StrictModel
from ap_agent.contracts.enums import InputCheck

__all__ = ["InputFlag", "OutputFlag"]


class InputFlag(StrictModel):
    """One reason this document goes to a person before any model sees it."""

    check: InputCheck
    detail: str = Field(
        min_length=1,
        max_length=200,
        description="Where and how much, in this system's own words. Never document text.",
    )


class OutputFlag(StrictModel):
    """One output-filter pattern that fired on one field of a model's reading.

    ``check`` is the pattern's name from the guardrails file, or
    ``suspicious_text`` when the reader itself reported instruction-like text.
    ``field`` is a path into the extraction (``payment_terms``,
    ``line_items.description``). The matched text is deliberately absent.
    """

    check: str = Field(min_length=1, max_length=64)
    field: str = Field(min_length=1, max_length=64)
