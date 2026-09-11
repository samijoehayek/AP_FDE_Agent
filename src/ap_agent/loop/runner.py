"""The orchestrator: decide, apply, transition, log.

Four properties are worth defending, because each one is a deliberate choice
against an easier alternative.

**`decide` never calls anything.** It is a pure function of the record, so the
whole routing policy can be tested without a network, a database or a key. The
easy alternative - deciding and doing in one function - makes every routing test
an integration test, and routing is the part most likely to change.

**`transition` is the only thing that changes state.** `apply` returns a record
carrying new *data* and a result carrying an *event*; it never sets `state`. The
state that follows an event is the transition table's decision alone. That is
what stops the loop from quietly inventing a path the table forbids, and it is
why a state that needs a human simply halts the run: the table refuses, and
there is no second opinion to fall back on.

**Stub steps are visible in the table, not hidden in the loop.** A stub advances
the state through `STUB_TRANSITIONS`, which is enumerable and tested. Had the
loop faked the state change itself, "what is real" would be a question you
answer by reading control flow instead of a table.

**Every step writes exactly one audit event, including the failures.** A step
that errors, a run that hits its budget, a state the table will not leave - all
of them are recorded. A trail with holes exactly where things went wrong is
worse than no trail, because it looks complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from ap_agent.contracts.audit import (
    AuditEvent,
    HumanActor,
    ModelActor,
    RuleActor,
    ToolActor,
    utc_now,
)
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.run import (
    Action,
    ActionKind,
    InvoiceRecord,
    StepResult,
    ToolCallRecord,
)
from ap_agent.errors import APAgentError, IllegalTransition
from ap_agent.logging import get_logger
from ap_agent.loop.dates import resolve_date_by_locale, resolve_date_by_receipt_window
from ap_agent.states.machine import InvoiceEvent, InvoiceState, is_terminal, transition
from ap_agent.tools.compute_extraction_confidence import (
    LOAD_BEARING_FIELDS,
    ComputeExtractionConfidenceInput,
    ComputeExtractionConfidenceOutput,
    DateResolutionReason,
    ExtractionConfidence,
    compute_extraction_confidence,
)
from ap_agent.tools.extract_invoice_text import (
    ExtractInvoiceTextInput,
    ExtractInvoiceTextOutput,
    extract_invoice_text,
)
from ap_agent.tools.extract_invoice_vision import (
    ExtractInvoiceVisionInput,
    ExtractInvoiceVisionOutput,
    extract_invoice_vision,
)
from ap_agent.tools.ingest_document import (
    IngestDocumentInput,
    IngestDocumentOutput,
    ingest_document,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ap_agent.audit.writer import AuditWriter
    from ap_agent.contracts.audit import Actor

log = get_logger(__name__)

RULE_CONFIG_VERSION = "v1"
"""The guardrails version a rule actor is recorded under."""

VALIDATE_RULE_ID = "VALIDATE@v1"
INGEST_RULE_ID = "INGEST@v1"
EXTRACT_CONF_RULE_ID = "EXTRACT-CONF@v1"
DATE_RESOLVE_RULE_ID = "DATE-RESOLVE@v1"
STUB_RULE_ID = "STUB"

VENDOR_LOCALE_DATE_HOOK = "vendor_country_date_resolution"
"""Name of the one thing a stub step actually does. TEMPORARY."""

STUB_HOOKS: frozenset[str] = frozenset({VENDOR_LOCALE_DATE_HOOK})
"""Behaviour performed inside a stub step, rather than by the tool that will own it.

A stub step is supposed to do nothing. This one does something - it settles an
open invoice date from the vendor's country - because ``lookup_vendor`` does not
exist yet and the alternative is carrying an open date past the only state that
can close it. Listed here, and asserted in
``tests/states/test_stub_transitions.py``, for the same reason the stub *edges*
are listed: so that deleting it when ``lookup_vendor`` becomes real is a
deliberate act with a failing test to confirm it.
"""

MAX_BASIS_CHARS = 1000
"""The audit contract's limit for ``decision_basis``. Sliced, never overflowed."""

NO_SECOND_READ = "no_second_read"
"""Recorded when the document had no text layer, so only one model read it."""

DEFAULT_MAX_STEPS = 15
"""Enough for the whole stubbed happy path (14 steps) and no more.

A budget is not optional. An agent that can loop forever will eventually loop
forever on the invoice that costs the most tokens.
"""


def _default_ingest(payload: IngestDocumentInput) -> IngestDocumentOutput:
    """Dispatch through the module attribute so the seam is patchable.

    A bare function as a dataclass default is bound when the class is created,
    which makes it invisible to ``monkeypatch.setattr(module, name, ...)`` - a
    test that tried would pass while still calling the real thing. Looking the
    name up at call time keeps the injection point honest.
    """
    return ingest_document(payload)


def _default_extract(payload: ExtractInvoiceVisionInput) -> ExtractInvoiceVisionOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return extract_invoice_vision(payload)


def _default_extract_text(payload: ExtractInvoiceTextInput) -> ExtractInvoiceTextOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return extract_invoice_text(payload)


def _default_confidence(
    payload: ComputeExtractionConfidenceInput,
) -> ComputeExtractionConfidenceOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return compute_extraction_confidence(payload)


@dataclass(frozen=True)
class RunContext:
    """Everything the loop needs from the outside world.

    The tools arrive as callables rather than being imported at the call site,
    so a test can substitute the extraction seat without patching a module and
    without any risk of reaching the real API.
    """

    run_id: str
    writer: AuditWriter
    ingest: Callable[[IngestDocumentInput], IngestDocumentOutput] = _default_ingest
    extract: Callable[[ExtractInvoiceVisionInput], ExtractInvoiceVisionOutput] = _default_extract
    extract_text: Callable[[ExtractInvoiceTextInput], ExtractInvoiceTextOutput] = (
        _default_extract_text
    )
    confidence: Callable[[ComputeExtractionConfidenceInput], ComputeExtractionConfidenceOutput] = (
        _default_confidence
    )
    now: Callable[[], datetime] = utc_now
    events: list[AuditEvent] = field(default_factory=list[AuditEvent])
    """Every event written this run, in order. Convenience for callers and tests."""


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


def decide(record: InvoiceRecord) -> Action:
    """Choose the next step. Calls nothing, reads only the record.

    The default arm is the important one: any state whose tool is unwritten gets
    a stub rather than an error, so the pipeline's shape can be exercised before
    its parts exist. What stops a stub from walking through a human decision is
    the transition table, not this function.
    """
    match record.state:
        case InvoiceState.RECEIVED:
            return Action(kind=ActionKind.INGEST_DOCUMENT, by="rule", rule_id=INGEST_RULE_ID)
        case InvoiceState.INGESTED:
            # One step, three calls: two readings and the check that scores
            # them. The step is attributed to the rule that decides on the
            # score, not to either model - neither model decides anything.
            return Action(
                kind=ActionKind.COMPUTE_EXTRACTION_CONFIDENCE,
                by="rule",
                rule_id=EXTRACT_CONF_RULE_ID,
            )
        case InvoiceState.EXTRACTED:
            return Action(kind=ActionKind.VALIDATE, by="rule", rule_id=VALIDATE_RULE_ID)
        case _:
            return Action(kind=ActionKind.STUB, by="rule", rule_id=STUB_RULE_ID)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


ZERO_TOTAL_FLAG = "total_is_zero"
FUTURE_DATE_FLAG = "invoice_date_in_the_future"
NO_EXTRACTION_FLAG = "no_extraction"

LOOP_ONLY_FLAGS: frozenset[str] = frozenset({ZERO_TOTAL_FLAG, FUTURE_DATE_FLAG, NO_EXTRACTION_FLAG})
"""Checks this step adds on top of the ones the contract already made.

Deliberately short. Anything that is a pure function of the extracted numbers
belongs on the contract, not here - see :func:`validate_extraction`.
"""


def validate_extraction(record: InvoiceRecord, now: datetime) -> tuple[list[str], str]:
    """Decide whether the extraction may advance. Returns ``(flags, event)``.

    The split between this and ``InvoiceExtraction``'s own arithmetic check is
    the interesting part, and it is a split by *kind of question*, not by
    convenience:

    **The contract answers questions about the numbers.** Do the lines sum to the
    subtotal, does subtotal plus tax equal the total, is a line's extension
    consistent with its quantity and price. Those are pure functions of the
    extraction, they are the same answer forever, and they belong wherever the
    extraction goes - so the contract computes them at construction and every
    consumer can read ``arithmetic_flags`` without re-deriving anything.

    **This function answers whether the invoice may proceed.** That is policy,
    and policy is allowed to depend on things a contract must not: the clock, and
    later the versioned guardrails config.

    The clock is the load-bearing half of that distinction. A validator that
    consults ``now`` cannot live on the contract, because re-loading a stored
    extraction next year would produce different flags than when it was written -
    which would break replay and, once the audit chain hashes it, would look
    exactly like tampering.

    So this reads the contract's findings rather than recomputing them. There is
    one implementation of each arithmetic rule and one set of tolerance
    constants; changing a tolerance changes both the contract and this decision,
    because they are the same code.

    Never raises. A document whose numbers disagree is precisely the document a
    human needs to see, and losing it to an exception would lose the evidence.
    """
    extraction = record.extraction
    if extraction is None:
        return [NO_EXTRACTION_FLAG], InvoiceEvent.VALIDATION_FAILED.value

    # Computed by InvoiceExtraction at construction. Recomputing them here would
    # be a second implementation of the same rules with its own copy of the
    # tolerances - two things that agree today and drift the first time one is
    # edited alone.
    flags = [flag.value for flag in extraction.arithmetic_flags]

    # A zero total is arithmetically fine and commercially meaningless. The
    # contract records a *negative* total as an observation; whether zero blocks
    # an invoice is a policy call, so it is made here. (A negative total is a
    # credit note, which is a real thing - it is flagged, not forbidden.)
    if extraction.total == 0:
        flags.append(ZERO_TOTAL_FLAG)

    # Time-dependent, so it cannot live on the contract. A future invoice date is
    # either a misread or a document that should not be paid yet; neither is
    # something to decide automatically.
    #
    # While the date is still open the check runs against every candidate and
    # fails only if all of them fail. An open date is not a validation failure -
    # a later state settles it - so flagging on one impossible reading would
    # reject invoices for a question nobody has answered yet.
    today = now.astimezone(UTC).date()
    candidates = record.date_candidates if record.date_is_open else [effective_date(record)]
    if all(candidate > today for candidate in candidates):
        flags.append(FUTURE_DATE_FLAG)

    event = InvoiceEvent.VALIDATE if not flags else InvoiceEvent.VALIDATION_FAILED
    return flags, event.value


def apply(
    action: Action, record: InvoiceRecord, ctx: RunContext
) -> tuple[InvoiceRecord, StepResult]:
    """Carry out ``action`` and return the new record and what happened.

    Never sets ``record.state``. The returned record carries new *data*; the
    returned result carries an *event*, and only ``transition`` turns that event
    into a state.
    """
    match action.kind:
        case ActionKind.INGEST_DOCUMENT:
            output = ctx.ingest(IngestDocumentInput(path=record.source_path))
            return (
                record.model_copy(update={"ingest": output}),
                StepResult(event=InvoiceEvent.INGEST.value, output_ref=f"sha256:{output.sha256}"),
            )

        case ActionKind.COMPUTE_EXTRACTION_CONFIDENCE:
            return _read_and_score(record, ctx)

        case ActionKind.VALIDATE:
            return _validate_step(record, ctx)

        case _:
            return _stub_step(record)


def effective_date(record: InvoiceRecord) -> date:
    """The invoice date downstream should use.

    The resolved date once something has settled it, and the extraction's own
    reading before that. Never ``None``: callers that need to know whether the
    question is still open ask ``record.date_is_open``, which is a different
    question from "what date do I print".
    """
    if record.invoice_date_resolved is not None:
        return record.invoice_date_resolved
    if record.extraction is not None:
        return record.extraction.invoice_date
    return date.min


def _read_and_score(record: InvoiceRecord, ctx: RunContext) -> tuple[InvoiceRecord, StepResult]:
    """Read the document twice, score the agreement, and decide who sees it next.

    Only the path is passed to either reader. Nothing read from the document -
    and in particular never ``remit_to_display`` - is fed back into a later
    call, and neither reader is given the other's answer: two readings that can
    see each other are one reading.
    """
    vision = ctx.extract(ExtractInvoiceVisionInput(path=record.source_path))
    text = ctx.extract_text(ExtractInvoiceTextInput(path=record.source_path))

    verdict = ctx.confidence(
        ComputeExtractionConfidenceInput(
            primary=vision.extraction,
            secondary=text.second_read,
            raw_text=text.raw_text,
            # Not known yet. The vendor master is read two states from here, and
            # guessing the country off the document is exactly what rule 2
            # forbids for bank details and is no better reasoning here.
            vendor_country=record.vendor_country,
            min_sharpness=record.ingest.min_sharpness if record.ingest else None,
        )
    ).confidence

    calls = [
        ToolCallRecord(
            kind=ActionKind.EXTRACT_INVOICE_VISION,
            by="model",
            event_type=AuditEventType.MODEL_CALL,
            summary="read",
            output_ref=f"invoice:{vision.extraction.invoice_number}",
            model_id=vision.model_id,
            prompt_version=vision.prompt_version,
            input_tokens=vision.input_tokens,
            output_tokens=vision.output_tokens,
            latency_ms=vision.latency_ms,
        ),
        ToolCallRecord(
            kind=ActionKind.EXTRACT_INVOICE_TEXT,
            by="model",
            event_type=AuditEventType.MODEL_CALL,
            summary="read" if text.second_read else NO_SECOND_READ,
            output_ref=f"chars:{len(text.raw_text)}",
            model_id=text.model_id,
            prompt_version=text.prompt_version,
            input_tokens=text.input_tokens,
            output_tokens=text.output_tokens,
            latency_ms=text.latency_ms,
        ),
        ToolCallRecord(
            kind=ActionKind.COMPUTE_EXTRACTION_CONFIDENCE,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="auto_ok" if verdict.auto_ok else "needs_human",
            output_ref=f"fields:{len(verdict.fields)}",
        ),
    ]

    event = InvoiceEvent.EXTRACT if verdict.auto_ok else InvoiceEvent.EXTRACTION_FAILED
    return (
        record.model_copy(
            update={
                "extraction": vision.extraction,
                "confidence": verdict,
                "invoice_date_resolved": verdict.resolved_invoice_date,
                "date_resolution_reason": verdict.date_resolution_reason,
            }
        ),
        StepResult(
            event=event.value,
            output_ref=f"invoice:{vision.extraction.invoice_number}",
            decision_basis=confidence_basis(verdict),
            calls=calls,
        ),
    )


def confidence_basis(verdict: ExtractionConfidence) -> str:
    """Summarise a verdict for one audit row: every load-bearing field, and the date.

    The row has to answer "why did this go to a person" - or "why did it not" -
    without anyone opening the invoice. Supporting fields are left out: they are
    scored, they never block, and including them would push the load-bearing
    ones off the end of the field.
    """
    parts = [
        f"{entry.field}={entry.score:.2f}/{entry.reason}"
        for entry in verdict.fields
        if entry.field in LOAD_BEARING_FIELDS
    ]
    parts.append(f"date_verdict={verdict.date_verdict.value}")
    if verdict.date_candidates:
        parts.append("candidates=" + "|".join(day.isoformat() for day in verdict.date_candidates))
    if verdict.date_resolution_reason is not None:
        parts.append(f"date_reason={verdict.date_resolution_reason.value}")
    if verdict.needs_human:
        parts.append(
            "needs_human=" + "|".join(f"{item.field}:{item.reason}" for item in verdict.needs_human)
        )
    return ", ".join(parts)[:MAX_BASIS_CHARS]


def _validate_step(record: InvoiceRecord, ctx: RunContext) -> tuple[InvoiceRecord, StepResult]:
    """Settle the date if the receipt window can, then decide whether to advance."""
    record, calls = _settle_by_receipt_window(record)
    flags, event = validate_extraction(record, ctx.now())
    basis = ", ".join(flags) if flags else f"date={effective_date(record).isoformat()}"
    if record.date_is_open:
        basis = f"{basis}, date_open"
    return (
        record.model_copy(update={"validation_flags": flags}),
        StepResult(
            event=event,
            output_ref=f"flags:{len(flags)}",
            decision_basis=basis[:MAX_BASIS_CHARS],
            calls=calls,
        ),
    )


def _settle_by_receipt_window(
    record: InvoiceRecord,
) -> tuple[InvoiceRecord, list[ToolCallRecord]]:
    """Close an open date when only one candidate could have arrived when it did.

    Needs nothing external, so it runs before the vendor is known and settles
    most ambiguous dates on its own. Leaves the date open when it cannot decide;
    that is not a failure and does not stop the invoice.
    """
    if not record.date_is_open or record.received_at is None:
        return record, []

    received = record.received_at.astimezone(UTC).date()
    candidates = record.date_candidates
    chosen = resolve_date_by_receipt_window(candidates, received)
    if chosen is None:
        return record, []

    basis = (
        "candidates=" + "|".join(day.isoformat() for day in candidates) + ", "
        f"received_at={received.isoformat()}, chose={chosen.isoformat()}, "
        f"reason={DateResolutionReason.RECEIPT_WINDOW.value}"
    )
    call = ToolCallRecord(
        kind=ActionKind.VALIDATE,
        by="rule",
        rule_id=DATE_RESOLVE_RULE_ID,
        event_type=AuditEventType.RULE_EVALUATION,
        summary=DateResolutionReason.RECEIPT_WINDOW.value[:64],
        output_ref=f"date:{chosen.isoformat()}",
        basis=basis[:MAX_BASIS_CHARS],
    )
    return (
        record.model_copy(
            update={
                "invoice_date_resolved": chosen,
                "date_resolution_reason": DateResolutionReason.RECEIPT_WINDOW,
            }
        ),
        [call],
    )


def _stub_step(record: InvoiceRecord) -> tuple[InvoiceRecord, StepResult]:
    """A step whose tool is unwritten. Succeeds vacuously, with one exception.

    The exception is :data:`VENDOR_LOCALE_DATE_HOOK`. ``lookup_vendor`` is not
    written, so nothing else will ever put a country on the record, and an open
    date would sail past the only state that can close it and reach the ERP
    unanswered. Delete this branch the moment ``lookup_vendor`` is real - the
    stub-hook test is there to make sure that is noticed.
    """
    if record.state is not InvoiceState.VENDOR_RESOLVED or not record.date_is_open:
        return record, StepResult(event=InvoiceEvent.STUB_OK.value)

    confidence = record.confidence
    raw_text = confidence.date_raw_text if confidence else None
    chosen = resolve_date_by_locale(raw_text, record.vendor_country)
    if chosen is None:
        return record, StepResult(
            event=InvoiceEvent.STUB_OK.value, decision_basis="date_open, no_vendor_country"
        )

    basis = (
        "candidates=" + "|".join(day.isoformat() for day in record.date_candidates) + ", "
        f"vendor_country={record.vendor_country}, chose={chosen.isoformat()}, "
        f"reason={DateResolutionReason.VENDOR_LOCALE.value}"
    )
    call = ToolCallRecord(
        kind=ActionKind.LOOKUP_VENDOR,
        by="rule",
        rule_id=DATE_RESOLVE_RULE_ID,
        event_type=AuditEventType.RULE_EVALUATION,
        summary=DateResolutionReason.VENDOR_LOCALE.value[:64],
        output_ref=f"date:{chosen.isoformat()}",
        basis=basis[:MAX_BASIS_CHARS],
    )
    return (
        record.model_copy(
            update={
                "invoice_date_resolved": chosen,
                "date_resolution_reason": DateResolutionReason.VENDOR_LOCALE,
            }
        ),
        StepResult(
            event=InvoiceEvent.STUB_OK.value,
            decision_basis=basis[:MAX_BASIS_CHARS],
            calls=[call],
        ),
    )


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepTransition:
    """Where one event sat in the run: which move, and what kind of event.

    Grouped rather than passed as loose arguments, which keeps the audit helpers
    at a signature a reader can hold in their head and makes it impossible to
    swap ``from_state`` and ``to_state`` at a call site.

    No sequence number. ``step_seq`` is the position of the event in the run's
    trail and is allocated where events are written, because a step can now emit
    several - two readings, a check, then the decision - and a number chosen by
    the caller would have to be threaded through every one of them to stay
    monotonic.
    """

    from_state: InvoiceState
    to_state: InvoiceState | None
    event_type: AuditEventType = AuditEventType.STATE_TRANSITION


def actor_for(action: Action, result: StepResult) -> Actor:
    """Derive the audit actor from who the action said was accountable.

    A step that *is* one call is attributed to the callee, because "which tool
    ran" is the answer an auditor wants and the rule id is already implied by
    the step. A step that weighed several calls - each of which got its own row
    naming its own tool - is attributed to the rule that weighed them, because
    that rule is what decided, and attributing a decision to one of its inputs
    would misname who is accountable.

    Neither reading model is ever the decider. They are asked what the document
    says; the rule decides what to do about the answer.
    """
    if action.by == "model":
        return ModelActor(
            model_id=result.model_id or "unknown",
            prompt_version=result.prompt_version or "unknown",
        )
    if action.by == "human":
        return HumanActor(user_id="unknown")
    if not result.calls and action.kind not in (ActionKind.VALIDATE, ActionKind.STUB):
        return ToolActor(name=action.kind.value)
    return RuleActor(rule_id=action.rule_id or STUB_RULE_ID, config_version=RULE_CONFIG_VERSION)


def make_event(
    record: InvoiceRecord,
    action: Action,
    result: StepResult,
    step: StepTransition,
    ctx: RunContext,
) -> AuditEvent:
    """Build the audit event for one step.

    ``prev_event_hash`` is left at its default: the writer owns the chain and
    overwrites it. A caller that chose its own predecessor would make the chain
    decorative.

    ``step_seq`` is this event's position in the run's trail - one row per thing
    that happened, which is what the audit contract asks for.
    """
    return AuditEvent(
        invoice_id=str(record.invoice_id),
        run_id=ctx.run_id,
        step_seq=len(ctx.events),
        ts_utc=ctx.now(),
        actor=actor_for(action, result),
        event_type=step.event_type,
        from_state=step.from_state,
        to_state=step.to_state,
        output_ref=result.output_ref,
        tool_name=action.kind.value,
        tool_result_summary=None if result.error else result.event,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        decision=result.event,
        decision_basis=result.decision_basis or ", ".join(record.validation_flags) or None,
        error_class=type(APAgentError).__name__ if result.error else None,
        error_message=result.error,
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run(
    record: InvoiceRecord, ctx: RunContext, max_steps: int = DEFAULT_MAX_STEPS
) -> InvoiceRecord:
    """Advance one invoice as far as it can go without a human.

    Stops on any of: a terminal state, a step that raised, a state the table
    will not leave, or the step budget. Every one of those writes an event
    before returning - a run that ends silently is indistinguishable from one
    that never started.

    Never raises. A failed invoice is a routing outcome, not a crash: the caller
    gets a record whose state says where it stopped.
    """
    step_seq = 0

    while step_seq < max_steps:
        if is_terminal(record.state):
            break

        from_state = record.state
        action = decide(record)

        try:
            record, result = apply(action, record, ctx)
        except Exception as exc:
            result = StepResult(event="error", error=f"{type(exc).__name__}: {exc}")
            step = StepTransition(from_state, None, AuditEventType.ERROR)
            _write(ctx, record, action, result, step)
            log.exception("step_failed", invoice_id=str(record.invoice_id))
            return record

        try:
            to_state = transition(from_state, result.event)
        except IllegalTransition as exc:
            # Not a bug: the table refusing a stub is how a state that needs a
            # human stops the machine. Recorded, then the run ends.
            result = result.model_copy(update={"error": str(exc)})
            step = StepTransition(from_state, None, AuditEventType.NOTE)
            _write(ctx, record, action, result, step)
            log.info("awaiting_human", invoice_id=str(record.invoice_id), state=from_state.value)
            return record

        _write(ctx, record, action, result, StepTransition(from_state, to_state))
        record = record.model_copy(update={"state": to_state})
        step_seq += 1

    if not is_terminal(record.state):
        _escalate(ctx, record, max_steps)

    return record


def _write(
    ctx: RunContext,
    record: InvoiceRecord,
    action: Action,
    result: StepResult,
    step: StepTransition,
) -> None:
    """Write one step's audit rows: every call it made, then what it decided.

    Calls first, and none of them carries a ``to_state``. Only the last row of a
    step moves the invoice, so a reader following ``to_state`` down the file sees
    the state machine and nothing else, while the rows in between account for
    what each call cost.
    """
    for call in result.calls:
        _emit(
            ctx,
            record,
            Action(kind=call.kind, by=call.by, rule_id=call.rule_id),
            StepResult(
                event=call.summary,
                output_ref=call.output_ref,
                decision_basis=call.basis,
                model_id=call.model_id,
                prompt_version=call.prompt_version,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                latency_ms=call.latency_ms,
            ),
            StepTransition(step.from_state, None, call.event_type),
        )
    _emit(ctx, record, action, result, step)


def _emit(
    ctx: RunContext,
    record: InvoiceRecord,
    action: Action,
    result: StepResult,
    step: StepTransition,
) -> None:
    """Build, write and remember one audit event."""
    ctx.events.append(ctx.writer.append(make_event(record, action, result, step, ctx)))


def _escalate(ctx: RunContext, record: InvoiceRecord, max_steps: int) -> None:
    """Record that the run stopped because it ran out of budget, not because it finished."""
    action = Action(kind=ActionKind.STUB, by="rule", rule_id="MAX_STEPS")
    result = StepResult(
        event="escalated",
        error=f"stopped after {max_steps} steps in {record.state.value}",
    )
    step = StepTransition(record.state, None, AuditEventType.ERROR)
    _write(ctx, record, action, result, step)
    log.warning(
        "max_steps_reached",
        invoice_id=str(record.invoice_id),
        state=record.state.value,
        max_steps=max_steps,
    )
