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

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field
from ulid import ULID

from ap_agent.contracts.common import StrictModel
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.states.machine import InvoiceState
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

    ingest: IngestDocumentOutput | None = None
    extraction: InvoiceExtraction | None = None
    validation_flags: list[str] = Field(
        default_factory=list[str],
        description="Why validation failed, in the order found. Empty means it passed.",
    )
