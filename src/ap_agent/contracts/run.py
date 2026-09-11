"""What one invoice looks like as it moves through the loop.

Three shapes, kept apart on purpose.

``InvoiceRecord`` is the invoice's state and everything gathered about it so
far. It is immutable: every step returns a new record via ``model_copy`` rather
than mutating in place, so a step that fails half-way cannot leave a record
partly updated, and the record handed to the audit writer is exactly the one the
step saw.

``Action`` is what the loop has decided to do next, decided *before* anything is
called. Separating the decision from the doing is what makes the loop testable
without a network: ``decide`` is a pure function of the record.

``StepResult`` is what came back. It carries the event name that drives the
state machine and the accounting - tokens, latency, model - that the audit trail
needs. Note that it carries an ``event``, not a state: only the transition table
turns an event into a state.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field
from ulid import ULID

from ap_agent.contracts.common import StrictModel
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.states.machine import InvoiceState
from ap_agent.tools.compute_extraction_confidence import (
    DateResolutionReason,
    ExtractionConfidence,
)
from ap_agent.tools.ingest_document import IngestDocumentOutput


class ActionKind(StrEnum):
    """What a step does.

    One member per tool, so an action names a real callable rather than a free
    string - ``tests/loop/test_runner.py`` asserts the two sets agree, so a tool
    added later cannot be silently unreachable from the loop.

    ``VALIDATE`` and ``STUB`` are the two exceptions. Validation is an in-process
    rule with no side effects, and ``STUB`` stands for every step whose tool is
    still unwritten.
    """

    INGEST_DOCUMENT = "ingest_document"
    EXTRACT_INVOICE_VISION = "extract_invoice_vision"
    EXTRACT_INVOICE_TEXT = "extract_invoice_text"
    COMPUTE_EXTRACTION_CONFIDENCE = "compute_extraction_confidence"
    LOOKUP_VENDOR = "lookup_vendor"
    GET_PURCHASE_ORDER = "get_purchase_order"
    GET_RECEIPTS = "get_receipts"
    FIND_DUPLICATES = "find_duplicates"
    COMPUTE_MATCH = "compute_match"
    CLASSIFY_EXCEPTION = "classify_exception"
    PROPOSE_GL_CODING = "propose_gl_coding"
    VERIFY_VENDOR_EXTERNAL = "verify_vendor_external"
    CREATE_BILL = "create_bill"
    REQUEST_APPROVAL = "request_approval"
    MARK_READY_FOR_PAYMENT = "mark_ready_for_payment"
    NOTIFY = "notify"

    VALIDATE = "validate"
    """An in-process arithmetic and sanity check. Not a tool: it calls nothing."""

    STUB = "stub"
    """A step whose tool does not exist yet. Advances the state and records that."""


class Action(StrictModel):
    """The decision, before anything is called.

    ``by`` is who is accountable for the step, and it becomes the actor on the
    audit event. It is the first question asked of an AP trail, so it is decided
    here rather than inferred later from what happened to run.
    """

    kind: ActionKind
    by: Literal["rule", "model", "human"]
    rule_id: str | None = Field(
        default=None,
        max_length=64,
        description="Required when `by` is 'rule'. Names the rule version that decided, so a "
        "past decision stays explainable under the rules that made it.",
    )


class ToolCallRecord(StrictModel):
    """One model or tool call made inside a step, recorded on its own audit row.

    A step that calls three things is three facts and then a decision. Folding
    them into the decision's row would lose what each call cost and which one
    was slow - and "what did the second reading cost" is a question an auditor
    asks about a system that pays two models to read the same page.

    Flat rather than a nested ``(Action, StepResult)`` pair, because the pair
    would make ``StepResult`` recursive for no gain: a call inside a call is not
    a thing this loop does.
    """

    kind: ActionKind
    by: Literal["rule", "model"]
    rule_id: str | None = Field(default=None, max_length=64)
    event_type: AuditEventType = AuditEventType.TOOL_CALL
    summary: str = Field(min_length=1, max_length=64, description="What happened, in one token.")
    output_ref: str | None = Field(default=None, max_length=512)
    basis: str | None = Field(
        default=None, max_length=1000, description="Why, for this call's own audit row."
    )

    model_id: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=32)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)


class StepResult(StrictModel):
    """What a step produced.

    ``event`` is a state-machine event, never a state. The loop hands it to
    ``transition()``, which is the only thing that may decide what state follows.
    """

    event: str = Field(min_length=1, max_length=64)
    output_ref: str | None = Field(
        default=None,
        max_length=512,
        description="Pointer to the full output. Payloads are not inlined into the trail.",
    )
    error: str | None = Field(default=None, max_length=2000)

    model_id: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=32)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)

    calls: list[ToolCallRecord] = Field(
        default_factory=list[ToolCallRecord],
        max_length=8,
        description="Calls this step made, in order. Each gets its own audit row, written "
        "before the row that records what the step decided.",
    )
    decision_basis: str | None = Field(
        default=None,
        max_length=1000,
        description="Why the step decided what it did, for the audit row. When a step has "
        "something better to say than a list of flags, it says it here.",
    )


class InvoiceRecord(StrictModel):
    """One invoice, and everything known about it so far.

    Frozen. Steps return a new record through ``model_copy(update=...)`` instead
    of mutating this one, which is what lets the audit event carry the record as
    it was when the step ran.
    """

    invoice_id: ULID = Field(default_factory=ULID)
    source_path: Path
    state: InvoiceState = InvoiceState.RECEIVED
    created_at: AwareDatetime

    received_at: AwareDatetime | None = Field(
        default=None,
        description="When the document arrived. Set by whoever handed it to the pipeline - an "
        "inbox timestamp, a scanner, an upload - and never by the loop, which would "
        "be reading its own clock and calling it evidence. Bounds an ambiguous "
        "invoice date: nothing can be issued after it was received.",
    )

    ingest: IngestDocumentOutput | None = None
    extraction: InvoiceExtraction | None = None
    confidence: ExtractionConfidence | None = Field(
        default=None,
        description="The verdict on the two readings. None before extraction has run.",
    )
    validation_flags: list[str] = Field(
        default_factory=list[str],
        description="Why validation failed, in the order found. Empty means it passed.",
    )

    invoice_date_resolved: date | None = Field(
        default=None,
        description="The effective invoice date. Set at extraction when the page was "
        "unambiguous, and later by whichever rule settled it. None while the date "
        "is still open.",
    )
    date_resolution_reason: DateResolutionReason | None = Field(
        default=None, description="Which rule settled the date. None when none had to."
    )
    vendor_country: str | None = Field(
        default=None,
        max_length=2,
        description="ISO-3166-1 alpha-2, from the vendor master - never read off the document. "
        "Unset until the vendor is resolved.",
    )

    @property
    def date_candidates(self) -> list[date]:
        """The invoice-date readings still live. Empty once the date is settled."""
        return list(self.confidence.date_candidates) if self.confidence else []

    @property
    def date_is_open(self) -> bool:
        """True while two readings of the invoice date are still possible."""
        return self.invoice_date_resolved is None and bool(self.date_candidates)

    @property
    def effective_invoice_date(self) -> date | None:
        """The date downstream should use, or None while it is still open."""
        return self.invoice_date_resolved
