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

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

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
from ap_agent.loop.dates import (
    MAX_INVOICE_AGE_DAYS,
    resolve_date_by_locale,
    resolve_date_by_receipt_window,
)
from ap_agent.pricing import UNKNOWN_PRICING_VERSION, cost_usd, pricing_version
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
from ap_agent.tools.get_purchase_order import (
    GetPurchaseOrderInput,
    GetPurchaseOrderOutput,
    get_purchase_order,
)
from ap_agent.tools.get_receipts import (
    GetReceiptsInput,
    GetReceiptsOutput,
    get_receipts,
)
from ap_agent.tools.ingest_document import (
    IngestDocumentInput,
    IngestDocumentOutput,
    ingest_document,
)
from ap_agent.tools.lookup_vendor import (
    LookupVendorInput,
    LookupVendorOutput,
    lookup_vendor,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from ap_agent.audit.writer import AuditWriter
    from ap_agent.contracts.audit import Actor
    from ap_agent.contracts.purchase_order import PurchaseOrder, ReceiptSet
    from ap_agent.contracts.vendor import VendorMatch

log = get_logger(__name__)

RULE_CONFIG_VERSION = "v1"
"""The guardrails version a rule actor is recorded under."""

VALIDATE_RULE_ID = "VALIDATE@v1"
INGEST_RULE_ID = "INGEST@v1"
EXTRACT_CONF_RULE_ID = "EXTRACT-CONF@v1"
DATE_RESOLVE_RULE_ID = "DATE-RESOLVE@v1"
RESOLVE_VENDOR_RULE_ID = "RESOLVE-VENDOR@v1"
MATCH_PREP_RULE_ID = "MATCH-PREP@v1"
STUB_RULE_ID = "STUB"

HALT_NO_TOOL = "halt_no_tool"
"""Decision recorded when the run stops because the next step has no tool yet."""

STUB_HOOKS: frozenset[str] = frozenset()
"""Behaviour performed inside a stub step, rather than by the tool that will own it.

Empty, and asserted empty in ``tests/states/test_stub_transitions.py``. A stub
step is supposed to do nothing; this set exists so that a stub which starts
doing something has to be written down, and so nobody has to read the loop to
find out what the scaffolding does.

It held one entry until ``lookup_vendor`` was written: the vendor's country was
settled inside the ``VENDOR_RESOLVED`` stub because nothing else would ever put
a country on the record. That is now the resolve-vendor step's job, which is
where it always belonged.
"""

MAX_BASIS_CHARS = 1000
"""The audit contract's limit for ``decision_basis``. Sliced, never overflowed."""

MAX_ERROR_CHARS = 2000
"""The audit contract's limit for ``error_message``."""

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


def _default_lookup_vendor(payload: LookupVendorInput) -> LookupVendorOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return lookup_vendor(payload)


def _default_get_purchase_order(payload: GetPurchaseOrderInput) -> GetPurchaseOrderOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return get_purchase_order(payload)


def _default_get_receipts(payload: GetReceiptsInput) -> GetReceiptsOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return get_receipts(payload)


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
    lookup_vendor: Callable[[LookupVendorInput], LookupVendorOutput] = _default_lookup_vendor
    get_purchase_order: Callable[[GetPurchaseOrderInput], GetPurchaseOrderOutput] = (
        _default_get_purchase_order
    )
    get_receipts: Callable[[GetReceiptsInput], GetReceiptsOutput] = _default_get_receipts
    now: Callable[[], datetime] = utc_now
    events: list[AuditEvent] = field(default_factory=list[AuditEvent])
    """Every event written this run, in order. Convenience for callers and tests."""


# ---------------------------------------------------------------------------
# the trail, written as it happens
# ---------------------------------------------------------------------------


@dataclass
class StepTrail:
    """Writes one audit row per tool, the moment that tool returns.

    The alternative - collecting rows on the ``StepResult`` and writing them when
    the step finishes - loses every row for a step that dies half way. A live run
    showed exactly that: an extraction step made two model calls, the second
    raised, and the trail recorded two rows for a run that had paid for three
    calls. The vision read had happened, had cost money, and left no trace.

    So the writing moved to where the calling happens. A failure is a row like
    any other, attributed to the tool that failed rather than to the step that
    contained it.
    """

    ctx: RunContext
    record: InvoiceRecord
    from_state: InvoiceState

    def call(self, call: ToolCallRecord) -> None:
        """Write one call's row now. Never carries a ``to_state``."""
        _emit(
            self.ctx,
            self.record,
            Action(kind=call.kind, by=call.by, rule_id=call.rule_id),
            StepResult(
                event=call.summary,
                output_ref=call.output_ref,
                result_summary=call.result_summary,
                decision_basis=call.basis,
                error=call.error,
                model_id=call.model_id,
                prompt_version=call.prompt_version,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                latency_ms=call.latency_ms,
                retry_count=call.retry_count,
            ),
            StepTransition(self.from_state, None, call.event_type),
        )

    def model_call(  # noqa: PLR0913 - one keyword per audit column, not a config object
        self,
        kind: ActionKind,
        output: BaseModel,
        *,
        summary: str,
        model_id: str | None,
        prompt_version: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int | None,
        retry_count: int = 0,
    ) -> None:
        """Write a model row, priced from the versioned list."""
        self.call(
            ToolCallRecord(
                kind=kind,
                by="model",
                event_type=AuditEventType.MODEL_CALL,
                summary=summary,
                output_ref=content_ref(output),
                result_summary=f"pricing={_pricing_version()}",
                model_id=model_id,
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                retry_count=retry_count,
            )
        )

    def failed(self, kind: ActionKind, by: Literal["rule", "model"], exc: Exception) -> None:
        """Write the row for a call that raised, naming the tool that raised it."""
        self.call(
            ToolCallRecord(
                kind=kind,
                by=by,
                event_type=AuditEventType.ERROR,
                summary="error",
                error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
            )
        )


def _pricing_version() -> str:
    """The price list's version, or a marker when it cannot be read.

    Never raises. A missing price list must not cost the system an audit row for
    a call that really happened.
    """
    try:
        return pricing_version()
    except APAgentError:
        return UNKNOWN_PRICING_VERSION


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
        case InvoiceState.VALIDATED:
            return Action(kind=ActionKind.LOOKUP_VENDOR, by="rule", rule_id=RESOLVE_VENDOR_RULE_ID)
        case InvoiceState.DUPLICATE_CHECKED:
            # One step, up to two calls: the order and what arrived against it.
            # The rule decides whether to ask at all, and what to do with the
            # answers - neither tool decides anything.
            return Action(kind=ActionKind.MATCH_PREP, by="rule", rule_id=MATCH_PREP_RULE_ID)
        case _:
            return Action(kind=ActionKind.STUB, by="rule", rule_id=STUB_RULE_ID)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


ZERO_TOTAL_FLAG = "total_is_zero"
FUTURE_DATE_FLAG = "invoice_date_in_the_future"
STALE_DATE_FLAG = "invoice_date_too_old"
NO_EXTRACTION_FLAG = "no_extraction"

LOOP_ONLY_FLAGS: frozenset[str] = frozenset(
    {ZERO_TOTAL_FLAG, FUTURE_DATE_FLAG, STALE_DATE_FLAG, NO_EXTRACTION_FLAG}
)
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

    # Staleness is measured against when the document *arrived*, not against the
    # clock: an invoice being processed late is not the same fact as an invoice
    # that was already ancient when it turned up, and only the second is a
    # reason to stop. Skipped entirely when nobody supplied a receipt date,
    # because inventing one would be the loop reading its own clock and calling
    # it evidence.
    if record.received_at is not None:
        received = record.received_at.astimezone(UTC).date()
        earliest = received - timedelta(days=MAX_INVOICE_AGE_DAYS)
        # Every candidate has to fail. While the date is open nobody has chosen
        # between the readings, so one impossible reading proves nothing - it may
        # simply be the reading that is wrong.
        if all(candidate < earliest for candidate in candidates):
            flags.append(STALE_DATE_FLAG)

    event = InvoiceEvent.VALIDATE if not flags else InvoiceEvent.VALIDATION_FAILED
    return flags, event.value


def apply(
    action: Action, record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Carry out ``action`` and return the new record and what happened.

    Never sets ``record.state``. The returned record carries new *data*; the
    returned result carries an *event*, and only ``transition`` turns that event
    into a state.

    ``trail`` is how a step records the calls it makes, as it makes them. A step
    that raises half way has already written rows for whatever it got through.
    """
    match action.kind:
        case ActionKind.INGEST_DOCUMENT:
            output = ctx.ingest(IngestDocumentInput(path=record.source_path))
            return (
                record.model_copy(update={"ingest": output}),
                StepResult(
                    event=InvoiceEvent.INGEST.value,
                    output_ref=content_ref(output),
                    result_summary=f"sha256={output.sha256[:12]}, pages={output.page_count}",
                ),
            )

        case ActionKind.COMPUTE_EXTRACTION_CONFIDENCE:
            return _read_and_score(record, ctx, trail)

        case ActionKind.VALIDATE:
            return _validate_step(record, ctx, trail)

        case ActionKind.LOOKUP_VENDOR:
            return _resolve_vendor_step(record, ctx, trail)

        case ActionKind.MATCH_PREP:
            return _match_prep_step(record, ctx, trail)

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


def _read_and_score(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Read the document twice, score the agreement, and decide who sees it next.

    Only the path is passed to either reader. Nothing read from the document -
    and in particular never ``remit_to_display`` - is fed back into a later
    call, and neither reader is given the other's answer: two readings that can
    see each other are one reading.

    Each call writes its own row as it returns. A reading that raises writes a
    row naming *itself*, and the rows for the readings that already succeeded
    stay on the trail - because they happened, and because they were paid for.
    """
    vision = _call(trail, ActionKind.EXTRACT_INVOICE_VISION, "model", ctx.extract)(
        ExtractInvoiceVisionInput(path=record.source_path)
    )
    trail.model_call(
        ActionKind.EXTRACT_INVOICE_VISION,
        vision,
        summary="read",
        model_id=vision.model_id,
        prompt_version=vision.prompt_version,
        input_tokens=vision.input_tokens,
        output_tokens=vision.output_tokens,
        latency_ms=vision.latency_ms,
        retry_count=vision.retry_count,
    )

    text = _call(trail, ActionKind.EXTRACT_INVOICE_TEXT, "model", ctx.extract_text)(
        ExtractInvoiceTextInput(path=record.source_path)
    )
    trail.model_call(
        ActionKind.EXTRACT_INVOICE_TEXT,
        text,
        summary="read" if text.second_read else NO_SECOND_READ,
        model_id=text.model_id,
        prompt_version=text.prompt_version,
        input_tokens=text.input_tokens,
        output_tokens=text.output_tokens,
        latency_ms=text.latency_ms,
        retry_count=text.retry_count,
    )

    scored = _call(trail, ActionKind.COMPUTE_EXTRACTION_CONFIDENCE, "rule", ctx.confidence)(
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
    )
    verdict = scored.confidence
    trail.call(
        ToolCallRecord(
            kind=ActionKind.COMPUTE_EXTRACTION_CONFIDENCE,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="auto_ok" if verdict.auto_ok else "needs_human",
            output_ref=content_ref(verdict),
            result_summary=f"fields={len(verdict.fields)}, needs_human={len(verdict.needs_human)}",
        )
    )

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
            output_ref=content_ref(vision.extraction),
            result_summary=f"invoice={vision.extraction.invoice_number}",
            decision_basis=confidence_basis(verdict),
        ),
    )


def _call(
    trail: StepTrail, kind: ActionKind, by: Literal["rule", "model"], fn: Callable[[Any], Any]
) -> Callable[[Any], Any]:
    """Wrap one tool call so that a failure still writes the failing tool's row.

    Without this a step that dies on its second call blames the *step* - which
    is how a live run recorded an extraction failure against
    ``compute_extraction_confidence``, a tool that had not run.
    """

    def invoke(payload: Any) -> Any:  # noqa: ANN401 - one wrapper for every tool signature
        try:
            return fn(payload)
        except Exception as exc:
            trail.failed(kind, by, exc)
            raise

    return invoke


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


def _validate_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Settle the date if the receipt window can, then decide whether to advance."""
    record = _settle_by_receipt_window(record, trail)
    flags, event = validate_extraction(record, ctx.now())
    return (
        record.model_copy(update={"validation_flags": flags}),
        StepResult(
            event=event,
            result_summary=f"flags={len(flags)}",
            decision_basis=_validate_basis(record, flags),
        ),
    )


def _validate_basis(record: InvoiceRecord, flags: list[str]) -> str:
    """Say what the date is, honestly, including when it is still two dates.

    Printing a single ``date=`` while the question is open states a fact nobody
    has established - the earlier trail said ``date=2023-07-03, date_open``,
    which reads as a date that happens to be flagged rather than as a choice
    nobody has made.
    """
    parts = list(flags)
    if record.date_is_open:
        parts.append("candidates=" + "|".join(day.isoformat() for day in record.date_candidates))
    else:
        parts.append(f"date={effective_date(record).isoformat()}")
    return ", ".join(parts)[:MAX_BASIS_CHARS]


def _settle_by_receipt_window(record: InvoiceRecord, trail: StepTrail) -> InvoiceRecord:
    """Close an open date when only one candidate could have arrived when it did.

    Needs nothing external, so it runs before the vendor is known and settles
    most ambiguous dates on its own. Leaves the date open when it cannot decide;
    that is not a failure and does not stop the invoice.
    """
    if not record.date_is_open or record.received_at is None:
        return record

    received = record.received_at.astimezone(UTC).date()
    candidates = record.date_candidates
    chosen = resolve_date_by_receipt_window(candidates, received)
    if chosen is None:
        return record

    basis = (
        "candidates=" + "|".join(day.isoformat() for day in candidates) + ", "
        f"received_at={received.isoformat()}, chose={chosen.isoformat()}, "
        f"reason={DateResolutionReason.RECEIPT_WINDOW.value}"
    )
    trail.call(
        ToolCallRecord(
            kind=ActionKind.VALIDATE,
            by="rule",
            rule_id=DATE_RESOLVE_RULE_ID,
            event_type=AuditEventType.RULE_EVALUATION,
            summary=DateResolutionReason.RECEIPT_WINDOW.value[:64],
            output_ref=content_ref(chosen.isoformat()),
            result_summary=f"date={chosen.isoformat()}",
            basis=basis[:MAX_BASIS_CHARS],
        )
    )
    return record.model_copy(
        update={
            "invoice_date_resolved": chosen,
            "date_resolution_reason": DateResolutionReason.RECEIPT_WINDOW,
        }
    )


VOLATILE_FIELDS: frozenset[str] = frozenset({"latency_ms", "input_tokens", "output_tokens"})
"""Excluded from a content address: they measure the call, not its answer.

``output_ref`` answers "what came back". How long it took and what it cost are
separate columns on the same row, and folding them into the hash would make the
same answer hash differently on every run - which destroys the one thing a
content address is for.
"""


def content_ref(value: BaseModel | str) -> str:
    """A content address for what a call returned, for ``output_ref``.

    The trail points at payloads, it does not inline them - an audit row is a
    claim about what happened, not a copy of it. Hashing what came back means the
    row identifies it exactly, so a snapshot stored elsewhere can later be proved
    to be the one that was acted on.

    Hashes the JSON rather than the object, because a ``repr`` carries no
    stability guarantee and a reference that moved when pydantic changed its
    formatting would report tampering where none happened.

    Human-readable notes do not go here. ``date=2024-03-09`` in an ``output_ref``
    is a label pretending to be an address; ``tool_result_summary`` exists for
    it.
    """
    if isinstance(value, str):
        payload = value
    else:
        payload = value.model_dump_json(exclude=set(VOLATILE_FIELDS))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _resolve_vendor_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Resolve the vendor against the master, then settle an open date with it.

    The order matters and is not arbitrary. The vendor's country is what decides
    a DD/MM against a MM/DD, and this is the first step that knows it - so the
    date question that the receipt window could not close is closed here, or
    stays open and is reported as open.

    A vendor that does not resolve routes to ``NEW_VENDOR`` and the run halts
    there, because no stub edge leaves that state. Onboarding a supplier is a
    human process with its own approval, and an invoice from a party nobody has
    approved is precisely the one a machine must not advance.

    Nothing read off the document selects the vendor except the name and tax id
    that were extracted. The country, the currency and the id all come back from
    the master.
    """
    extraction = record.extraction
    if extraction is None:
        # Unreachable on the real path: VALIDATED implies an extraction. Routed
        # rather than raised, because a crash here would lose the record.
        return record, StepResult(
            event=InvoiceEvent.VENDOR_NOT_FOUND.value, decision_basis=NO_EXTRACTION_FLAG
        )

    found = _call(trail, ActionKind.LOOKUP_VENDOR, "rule", ctx.lookup_vendor)(
        LookupVendorInput(
            vendor_name=extraction.vendor_name,
            vendor_tax_id=extraction.vendor_tax_id,
        )
    )
    match = found.match
    trail.call(
        ToolCallRecord(
            kind=ActionKind.LOOKUP_VENDOR,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary=match.match_basis.value[:64],
            output_ref=content_ref(match),
            result_summary=f"vendor={match.vendor_name or 'none'}",
            basis=_vendor_basis(extraction.vendor_name, match),
            latency_ms=found.latency_ms,
        )
    )

    if not match.resolved:
        return (
            record.model_copy(update={"vendor_match": match}),
            StepResult(
                event=InvoiceEvent.VENDOR_NOT_FOUND.value,
                output_ref=content_ref(match),
                decision_basis=_vendor_basis(extraction.vendor_name, match),
            ),
        )

    resolved = record.model_copy(
        update={
            "vendor_match": match,
            "vendor_id": match.vendor_id,
            "vendor_country": match.country,
            "vendor_currency": match.currency,
        }
    )
    resolved = _settle_by_vendor_locale(resolved, trail)

    return (
        resolved,
        StepResult(
            event=InvoiceEvent.RESOLVE_VENDOR.value,
            output_ref=content_ref(match),
            result_summary=f"vendor_id={match.vendor_id}, country={match.country}",
            decision_basis=_vendor_basis(extraction.vendor_name, match),
        ),
    )


def _vendor_basis(printed_name: str, match: VendorMatch) -> str:
    """Say what was searched for and what came back, in one line."""
    parts = [f"printed={printed_name!r}", f"basis={match.match_basis.value}"]
    if match.resolved:
        parts.append(f"vendor_id={match.vendor_id}, country={match.country}")
    elif match.candidates:
        parts.append(
            "candidates=" + "|".join(f"{c.vendor_id}:{c.vendor_name}" for c in match.candidates)
        )
    else:
        parts.append("candidates=none")
    return ", ".join(parts)[:MAX_BASIS_CHARS]


def _settle_by_vendor_locale(record: InvoiceRecord, trail: StepTrail) -> InvoiceRecord:
    """Close an open date from the resolved vendor's country.

    The second and last date rule. It re-reads the rendering the confidence
    check carried forward rather than choosing between the two candidate dates,
    because the candidates alone do not record which of them was day-first -
    ``{2024-03-09, 2024-09-03}`` is the same pair whether the page said
    ``09/03`` or ``03/09``.

    Leaves the date open when the country is one the locale table does not
    cover. That is reported, not guessed at: a consistent guess would be
    silently and reproducibly wrong.
    """
    if not record.date_is_open:
        return record

    confidence = record.confidence
    raw_text = confidence.date_raw_text if confidence else None
    chosen = resolve_date_by_locale(raw_text, record.vendor_country)
    if chosen is None:
        return record

    basis = (
        "candidates=" + "|".join(day.isoformat() for day in record.date_candidates) + ", "
        f"vendor_country={record.vendor_country}, chose={chosen.isoformat()}, "
        f"reason={DateResolutionReason.VENDOR_LOCALE.value}"
    )
    trail.call(
        ToolCallRecord(
            kind=ActionKind.LOOKUP_VENDOR,
            by="rule",
            rule_id=DATE_RESOLVE_RULE_ID,
            event_type=AuditEventType.RULE_EVALUATION,
            summary=DateResolutionReason.VENDOR_LOCALE.value[:64],
            output_ref=content_ref(chosen.isoformat()),
            result_summary=f"date={chosen.isoformat()}",
            basis=basis[:MAX_BASIS_CHARS],
        )
    )
    return record.model_copy(
        update={
            "invoice_date_resolved": chosen,
            "date_resolution_reason": DateResolutionReason.VENDOR_LOCALE,
        }
    )


def _match_prep_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Gather what the matcher will compare, and refuse to hand it anything unsafe.

    Three outcomes and no fourth:

    * **No PO reference on the document** - the invoice goes down the NON_PO
      path, which is a different pipeline with its own coding and approval, not
      a failed match. Nothing is fetched: there is nothing to fetch.
    * **The order is missing, or belongs to someone else** - an exception, and
      the matcher never sees it. The second of those is an identity check rather
      than a tolerance: an invoice quoting another supplier's PO number is a
      fraud signal, and a signal that gets compared against a band is a signal
      that can be argued into passing.
    * **The order is this vendor's** - fetch what arrived against it and advance
      with both snapshots on the record.

    ``compute_match`` is not called here and is still a stub. This step's job is
    to put the right facts in front of it, which includes deciding when there is
    no safe match to attempt.
    """
    extraction = record.extraction
    references = list(extraction.po_references) if extraction else []
    if not references:
        return record, StepResult(
            event=InvoiceEvent.NO_PO_REFERENCE.value,
            decision_basis="po_references=none",
        )

    # One order per invoice today. A document citing several is a real case and
    # a different shape of match; it is not this step's to invent.
    po_number = references[0]
    fetched = _call(trail, ActionKind.GET_PURCHASE_ORDER, "rule", ctx.get_purchase_order)(
        GetPurchaseOrderInput(po_number=po_number)
    )
    order = fetched.purchase_order
    trail.call(
        ToolCallRecord(
            kind=ActionKind.GET_PURCHASE_ORDER,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="found" if order else "not_found",
            output_ref=content_ref(order) if order else content_ref(f"po_not_found:{po_number}"),
            result_summary=f"po_number={po_number}, lines={len(order.lines) if order else 0}",
            basis=f"po_number={po_number}",
            latency_ms=fetched.latency_ms,
        )
    )

    if order is None:
        return (
            record,
            StepResult(
                event=InvoiceEvent.MATCH_EXCEPTION.value,
                output_ref=content_ref(f"po_not_found:{po_number}"),
                result_summary=f"po_number={po_number}",
                decision_basis=f"po_number={po_number}, po_not_found",
            ),
        )

    mismatch = _vendor_mismatch(record, order)
    if mismatch is not None:
        return (
            record.model_copy(update={"purchase_order": order}),
            StepResult(
                event=InvoiceEvent.MATCH_EXCEPTION.value,
                output_ref=content_ref(order),
                result_summary=f"po_number={po_number}",
                decision_basis=mismatch,
            ),
        )

    received = _call(trail, ActionKind.GET_RECEIPTS, "rule", ctx.get_receipts)(
        GetReceiptsInput(po_number=po_number)
    )
    trail.call(
        ToolCallRecord(
            kind=ActionKind.GET_RECEIPTS,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="nothing_received" if received.receipts.is_empty else "received",
            output_ref=content_ref(received.receipts),
            result_summary=f"lines={len(received.receipts.lines)}",
            basis=f"po_number={po_number}, lines={len(received.receipts.lines)}",
            latency_ms=received.latency_ms,
        )
    )

    return (
        record.model_copy(update={"purchase_order": order, "receipts": received.receipts}),
        StepResult(
            # compute_match is the owner's to write. Until it exists the invoice
            # advances through the stub edge with the real facts in context,
            # which is what makes the next session's work a matcher and not a
            # data-plumbing exercise.
            event=InvoiceEvent.STUB_OK.value,
            output_ref=content_ref(order),
            result_summary=f"po_number={po_number}",
            decision_basis=_match_prep_basis(order, received.receipts),
        ),
    )


def _vendor_mismatch(record: InvoiceRecord, order: PurchaseOrder) -> str | None:
    """Return why this order is not this invoice's to bill against, or None.

    An identity check, not a tolerance. A tolerance answers "is this close
    enough", and there is no close enough to belonging to a different supplier -
    an invoice quoting another vendor's purchase-order number is one of the
    oldest frauds in accounts payable, and a signal compared against a band is a
    signal that can be argued into passing.

    Compares ERP ids, never names. The name is what the document claimed, and
    the claim is the thing under suspicion.
    """
    if record.vendor_id is None or order.vendor_erp_id == record.vendor_id:
        return None
    return (
        f"po_number={order.po_number}, po_vendor={order.vendor_erp_id}, "
        f"invoice_vendor={record.vendor_id}, vendor_mismatch"
    )


def _match_prep_basis(order: PurchaseOrder, receipts: ReceiptSet) -> str:
    """One line saying what was gathered, for the decision's audit row."""
    return (
        f"po_number={order.po_number}, po_lines={len(order.lines)}, "
        f"receipt_lines={len(receipts.lines)}, status={order.status.value}"
    )[:MAX_BASIS_CHARS]


def _stub_step(record: InvoiceRecord) -> tuple[InvoiceRecord, StepResult]:
    """A step whose tool is unwritten. Succeeds vacuously and says so.

    It now does nothing at all, which is what a stub should do. It carried the
    vendor-locale date resolution until ``lookup_vendor`` was written; that
    moved to :func:`_resolve_vendor_step`, and ``STUB_HOOKS`` is empty again.
    """
    return record, StepResult(event=InvoiceEvent.STUB_OK.value)


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


_RULE_STEP_KINDS: frozenset[ActionKind] = frozenset(
    {
        ActionKind.VALIDATE,
        ActionKind.STUB,
        ActionKind.MATCH_PREP,
        ActionKind.COMPUTE_EXTRACTION_CONFIDENCE,
        ActionKind.LOOKUP_VENDOR,
    }
)
"""Kinds whose *step* is a decision over several calls rather than one call.

A step that **is** one call is attributed to the callee: "which tool ran" is what
an auditor wants, and ``ingest_document`` is not in this set for that reason. A
step that *weighed* calls - each of which wrote its own row naming its own tool -
is attributed to the rule that weighed them.

The two are told apart by ``rule_id``: a step row carries one, a call row does
not. That matters because a call row and its step row share a kind - the
``lookup_vendor`` call and the ``RESOLVE-VENDOR@v1`` decision are both
``LOOKUP_VENDOR`` - and without the second test every call row here would be
misattributed to a rule.
"""


def actor_for(
    action: Action, result: StepResult, event_type: AuditEventType | None = None
) -> Actor:
    """Derive the audit actor from who the action said was accountable.

    A step that *is* one call is attributed to the callee, because "which tool
    ran" is the answer an auditor wants and the rule id is already implied by
    the step. A step that weighed several calls - each of which got its own row
    naming its own tool - is attributed to the rule that weighed them, because
    that rule is what decided, and attributing a decision to one of its inputs
    would misname who is accountable.

    A row whose ``event_type`` is ``RULE_EVALUATION`` is a rule whatever kind it
    carries. The date-resolution rows are the case: they sit inside a step named
    after a tool, and naming that tool as their actor would say a lookup decided
    which of two readings of a date was right. It did not - a rule did, and the
    row names which one.

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
    if event_type is AuditEventType.RULE_EVALUATION:
        return RuleActor(rule_id=action.rule_id or STUB_RULE_ID, config_version=RULE_CONFIG_VERSION)
    if action.rule_id is None or action.kind not in _RULE_STEP_KINDS:
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
        actor=actor_for(action, result, step.event_type),
        event_type=step.event_type,
        from_state=step.from_state,
        to_state=step.to_state,
        output_ref=result.output_ref,
        tool_name=action.kind.value,
        tool_result_summary=result.result_summary,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        cost_usd=cost_usd(result.model_id, result.input_tokens, result.output_tokens),
        retry_count=result.retry_count,
        decision=result.event,
        decision_basis=result.decision_basis or ", ".join(record.validation_flags) or None,
        error_class=result.error_class,
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
        trail = StepTrail(ctx, record, from_state)

        try:
            record, result = apply(action, record, ctx, trail)
        except Exception as exc:
            # The failing tool has already written its own row through the trail.
            # This one records that the *step* stopped, and why.
            result = StepResult(
                event="error",
                error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
                error_class=type(exc).__name__,
            )
            _write(
                ctx, record, action, result, StepTransition(from_state, None, AuditEventType.ERROR)
            )
            log.exception("step_failed", invoice_id=str(record.invoice_id))
            return record

        try:
            to_state = transition(from_state, result.event)
        except IllegalTransition as exc:
            _write(ctx, record, action, *_halt(from_state, result, exc))
            log.info("awaiting_human", invoice_id=str(record.invoice_id), state=from_state.value)
            return record

        _write(ctx, record, action, result, StepTransition(from_state, to_state))
        record = record.model_copy(update={"state": to_state})
        step_seq += 1

    if not is_terminal(record.state):
        _escalate(ctx, record, max_steps)

    return record


def _halt(
    from_state: InvoiceState, result: StepResult, exc: IllegalTransition
) -> tuple[StepResult, StepTransition]:
    """Classify a refused transition. Two kinds, and only one of them is a bug.

    **The tool does not exist yet.** The loop asked for ``stub_ok`` from a state
    with no stub edge - an invoice parked at NON_PO because GL coding is
    unwritten, or at NEW_VENDOR because onboarding is a human process. That is
    the pipeline behaving as designed, and recording it as an error teaches a
    reader to skim past errors. It gets its own event type and no error class.

    **Anything else.** A real event the table refuses means the loop and the
    table disagree about the pipeline's shape, which is a defect and is recorded
    as one.
    """
    if result.event == InvoiceEvent.STUB_OK.value:
        return (
            result.model_copy(
                update={
                    "event": HALT_NO_TOOL,
                    "decision_basis": f"no tool for the step after {from_state.value}",
                }
            ),
            StepTransition(from_state, None, AuditEventType.HALT),
        )
    return (
        result.model_copy(
            update={"error": str(exc)[:MAX_ERROR_CHARS], "error_class": type(exc).__name__}
        ),
        StepTransition(from_state, None, AuditEventType.ERROR),
    )


def _write(
    ctx: RunContext,
    record: InvoiceRecord,
    action: Action,
    result: StepResult,
    step: StepTransition,
) -> None:
    """Write the row that records what a step decided.

    The calls a step made wrote their own rows through :class:`StepTrail` as they
    returned, so by the time this runs they are already on the trail - including
    the ones from a step that then failed. Only this row carries a ``to_state``,
    so a reader following ``to_state`` down the file sees the state machine and
    nothing else.
    """
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
