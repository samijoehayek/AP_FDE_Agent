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
from datetime import UTC, datetime
from decimal import Decimal
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
from ap_agent.contracts.invoice import (
    LINE_SUM_ABS_TOLERANCE,
    LINE_SUM_REL_TOLERANCE,
    TOTALS_TOLERANCE,
)
from ap_agent.contracts.run import Action, ActionKind, InvoiceRecord, StepResult
from ap_agent.errors import APAgentError, IllegalTransition
from ap_agent.logging import get_logger
from ap_agent.states.machine import InvoiceEvent, InvoiceState, is_terminal, transition
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
STUB_RULE_ID = "STUB"

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
            return Action(kind=ActionKind.EXTRACT_INVOICE_VISION, by="model")
        case InvoiceState.EXTRACTED:
            return Action(kind=ActionKind.VALIDATE, by="rule", rule_id=VALIDATE_RULE_ID)
        case _:
            return Action(kind=ActionKind.STUB, by="rule", rule_id=STUB_RULE_ID)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _validate(record: InvoiceRecord, now: datetime) -> tuple[list[str], str]:
    """Run the arithmetic and sanity checks. Returns ``(flags, event)``.

    Deterministic and total: it never raises, because a document whose numbers
    disagree is exactly the document a human needs to see, and losing it to an
    exception would be losing the evidence.
    """
    extraction = record.extraction
    if extraction is None:
        return ["no_extraction"], InvoiceEvent.VALIDATION_FAILED.value

    flags: list[str] = []

    if abs(extraction.subtotal + extraction.tax_total - extraction.total) > TOTALS_TOLERANCE:
        flags.append("totals_do_not_sum")

    if extraction.line_items:
        line_sum = sum((item.extended_price for item in extraction.line_items), Decimal(0))
        allowed = max(LINE_SUM_ABS_TOLERANCE, abs(extraction.subtotal) * LINE_SUM_REL_TOLERANCE)
        if abs(line_sum - extraction.subtotal) > allowed:
            flags.append("lines_do_not_sum_to_subtotal")
    else:
        flags.append("no_line_items")

    if extraction.total <= 0:
        flags.append("total_not_positive")

    # A future invoice date is either a misread or a document that should not be
    # paid yet. Either way it is not something to decide automatically.
    if extraction.invoice_date > now.astimezone(UTC).date():
        flags.append("invoice_date_in_the_future")

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

        case ActionKind.EXTRACT_INVOICE_VISION:
            # Only the path is passed. Nothing read from the document - and in
            # particular never remit_to_display - is fed back into a later call.
            result = ctx.extract(ExtractInvoiceVisionInput(path=record.source_path))
            return (
                record.model_copy(update={"extraction": result.extraction}),
                StepResult(
                    event=InvoiceEvent.EXTRACT.value,
                    output_ref=f"invoice:{result.extraction.invoice_number}",
                    model_id=result.model_id,
                    prompt_version=result.prompt_version,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                ),
            )

        case ActionKind.VALIDATE:
            flags, event = _validate(record, ctx.now())
            return (
                record.model_copy(update={"validation_flags": flags}),
                StepResult(event=event, output_ref=f"flags:{len(flags)}"),
            )

        case _:
            # Every other tool is unwritten. The step succeeds vacuously and says
            # so; the table decides whether that is allowed to move anything.
            return record, StepResult(event=InvoiceEvent.STUB_OK.value)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepTransition:
    """Where one step sat in the run: which move, and which number.

    Grouped rather than passed as four loose arguments, which keeps the audit
    helpers at a signature a reader can hold in their head and makes it
    impossible to swap ``from_state`` and ``to_state`` at a call site.
    """

    from_state: InvoiceState
    to_state: InvoiceState | None
    step_seq: int
    event_type: AuditEventType = AuditEventType.STATE_TRANSITION


def actor_for(action: Action, result: StepResult) -> Actor:
    """Derive the audit actor from who the action said was accountable.

    A rule action that calls a tool is attributed to the tool, because "which
    tool ran" is the answer an auditor wants and the rule id is already implied
    by the step. A rule action that calls nothing is attributed to the rule.
    """
    if action.by == "model":
        return ModelActor(
            model_id=result.model_id or "unknown",
            prompt_version=result.prompt_version or "unknown",
        )
    if action.by == "human":
        return HumanActor(user_id="unknown")
    if action.kind not in (ActionKind.VALIDATE, ActionKind.STUB):
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
    """
    return AuditEvent(
        invoice_id=str(record.invoice_id),
        run_id=ctx.run_id,
        step_seq=step.step_seq,
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
        decision_basis=", ".join(record.validation_flags) or None,
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
            step = StepTransition(from_state, None, step_seq, AuditEventType.ERROR)
            _write(ctx, record, action, result, step)
            log.exception("step_failed", invoice_id=str(record.invoice_id))
            return record

        try:
            to_state = transition(from_state, result.event)
        except IllegalTransition as exc:
            # Not a bug: the table refusing a stub is how a state that needs a
            # human stops the machine. Recorded, then the run ends.
            result = result.model_copy(update={"error": str(exc)})
            step = StepTransition(from_state, None, step_seq, AuditEventType.NOTE)
            _write(ctx, record, action, result, step)
            log.info("awaiting_human", invoice_id=str(record.invoice_id), state=from_state.value)
            return record

        _write(ctx, record, action, result, StepTransition(from_state, to_state, step_seq))
        record = record.model_copy(update={"state": to_state})
        step_seq += 1

    if not is_terminal(record.state):
        _escalate(ctx, record, step_seq, max_steps)

    return record


def _write(
    ctx: RunContext,
    record: InvoiceRecord,
    action: Action,
    result: StepResult,
    step: StepTransition,
) -> None:
    """Build, write and remember one audit event."""
    ctx.events.append(ctx.writer.append(make_event(record, action, result, step, ctx)))


def _escalate(ctx: RunContext, record: InvoiceRecord, step_seq: int, max_steps: int) -> None:
    """Record that the run stopped because it ran out of budget, not because it finished."""
    action = Action(kind=ActionKind.STUB, by="rule", rule_id="MAX_STEPS")
    result = StepResult(
        event="escalated",
        error=f"stopped after {max_steps} steps in {record.state.value}",
    )
    step = StepTransition(record.state, None, step_seq, AuditEventType.ERROR)
    _write(ctx, record, action, result, step)
    log.warning(
        "max_steps_reached",
        invoice_id=str(record.invoice_id),
        state=record.state.value,
        max_steps=max_steps,
    )
