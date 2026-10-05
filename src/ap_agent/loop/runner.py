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
from ap_agent.contracts.dedup import DedupEntry
from ap_agent.contracts.enums import AuditEventType, MatchLineOutcome, ReasonCode
from ap_agent.contracts.exceptions import HeaderNumbers, PoSnapshot, VendorSummary
from ap_agent.contracts.matching import MatchResult
from ap_agent.contracts.run import (
    Action,
    ActionKind,
    InvoiceRecord,
    StepResult,
    ToolCallRecord,
)
from ap_agent.errors import (
    APAgentError,
    ClassificationError,
    HumanDecisionError,
    IllegalTransition,
)
from ap_agent.guardrails.approval import approval_requirement
from ap_agent.guardrails.config import load_guardrails
from ap_agent.guardrails.output_filter import screen_extraction
from ap_agent.logging import get_logger
from ap_agent.loop.dates import resolve_date_by_locale, resolve_date_by_receipt_window
from ap_agent.payloads import (
    InMemoryPayloadStore,
    PayloadStore,
    store_ref,
)
from ap_agent.persistence import (
    CURRENT_APPROVAL_STORE,
    ApprovalDecisionRefusedError,
    ApprovalStore,
    DedupIndex,
    InMemoryApprovalStore,
    InMemoryDedupIndex,
    InvoiceStore,
)
from ap_agent.pricing import UNKNOWN_PRICING_VERSION, cost_usd, pricing_version
from ap_agent.states.machine import (
    HUMAN_ONLY_EVENTS,
    InvoiceEvent,
    InvoiceState,
    is_terminal,
    transition,
)
from ap_agent.text import normalise_invoice_number
from ap_agent.tools.classify_exception import (
    ClassifyExceptionInput,
    ClassifyExceptionOutput,
    classify_exception,
)
from ap_agent.tools.compute_extraction_confidence import (
    LOAD_BEARING_FIELDS,
    ComputeExtractionConfidenceInput,
    ComputeExtractionConfidenceOutput,
    DateResolutionReason,
    ExtractionConfidence,
    compute_extraction_confidence,
)
from ap_agent.tools.compute_match import (
    ComputeMatchInput,
    ComputeMatchOutput,
    compute_match,
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
from ap_agent.tools.find_duplicates import (
    FindDuplicatesInput,
    FindDuplicatesOutput,
    find_duplicates,
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
from ap_agent.tools.notify import NotifyInput, NotifyOutput, notify
from ap_agent.tools.request_approval import (
    RequestApprovalInput,
    RequestApprovalOutput,
    request_approval,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

    from pydantic import BaseModel

    from ap_agent.audit.writer import AuditWriter
    from ap_agent.contracts.audit import Actor
    from ap_agent.contracts.exceptions import ExceptionClassification
    from ap_agent.contracts.invoice import InvoiceExtraction
    from ap_agent.contracts.purchase_order import PurchaseOrder, ReceiptSet
    from ap_agent.contracts.vendor import VendorMatch

log = get_logger(__name__)

RULE_CONFIG_VERSION = "v1"
"""The guardrails version a rule actor is recorded under."""

VALIDATE_RULE_ID = "VALIDATE@v1"
INPUT_VALIDATE_RULE_ID = "INPUT-VALIDATE@v1"
OUTPUT_FILTER_RULE_ID = "OUTPUT-FILTER@v1"
EXTRACT_CONF_RULE_ID = "EXTRACT-CONF@v1"
DATE_RESOLVE_RULE_ID = "DATE-RESOLVE@v1"
RESOLVE_VENDOR_RULE_ID = "RESOLVE-VENDOR@v1"
MATCH_RULE_ID = "MATCH@v1"
CLASSIFY_RULE_ID = "CLASSIFY@v1"
DEDUP_RULE_ID = "DEDUP@v1"
APPROVAL_RULE_ID = "APPROVAL-MATRIX@v1"
APPROVAL_REFRESH_RULE_ID = "APPROVAL-REFRESH@v1"
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

DEFAULT_MAX_STEPS = 9
"""Enough for the longest stretch the loop walks on its own (8 steps) and no more.

That stretch is RECEIVED to PENDING_APPROVAL. Approval is a person now, so no
run goes from intake to CLOSED in one go: the walk after approval is a separate
run, and a shorter one.

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


def _default_compute_match(payload: ComputeMatchInput) -> ComputeMatchOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`.

    Injected like every other tool even though the matcher is pure, because the
    wrapper is not: it loads the guardrails file and refuses if the version it
    finds is not the one the run asked for. That is a read of the outside world,
    and this is where the outside world is substitutable.
    """
    return compute_match(payload)


def _default_find_duplicates(payload: FindDuplicatesInput) -> FindDuplicatesOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return find_duplicates(payload)


def _default_request_approval(payload: RequestApprovalInput) -> RequestApprovalOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return request_approval(payload)


def _default_notify(payload: NotifyInput) -> NotifyOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return notify(payload)


def _default_classify(payload: ClassifyExceptionInput) -> ClassifyExceptionOutput:
    """Dispatch through the module attribute. See :func:`_default_ingest`."""
    return classify_exception(payload)


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
    compute_match: Callable[[ComputeMatchInput], ComputeMatchOutput] = _default_compute_match
    classify: Callable[[ClassifyExceptionInput], ClassifyExceptionOutput] = _default_classify
    payloads: PayloadStore = field(default_factory=InMemoryPayloadStore)
    store: InvoiceStore | None = None
    """Durable invoice state. None runs in memory only - tests, and nothing else."""
    dedup: DedupIndex = field(default_factory=InMemoryDedupIndex)
    """What the duplicate check compares against. ``just run`` passes Postgres."""
    find_duplicates: Callable[[FindDuplicatesInput], FindDuplicatesOutput] = (
        _default_find_duplicates
    )
    approvals: ApprovalStore = field(default_factory=InMemoryApprovalStore)
    """Durable approval requests and decisions. ``just run`` passes Postgres."""
    request_approval: Callable[[RequestApprovalInput], RequestApprovalOutput] = (
        _default_request_approval
    )
    notify: Callable[[NotifyInput], NotifyOutput] = _default_notify
    """Where every ``output_ref`` is stored. In memory unless the caller says
    otherwise; ``just run`` passes the Postgres store."""
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

    def ref(self, value: BaseModel | str) -> str:
        """Store ``value`` as a payload, then return the reference that cites it.

        Every ``output_ref`` goes through here, so every one resolves. Stored
        before it is returned, and therefore before the row carrying it is
        written: a crash in between leaves an unused payload, never a dangling
        reference.
        """
        return store_ref(self.ctx.payloads, value)

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
        basis: str | None = None,
    ) -> str:
        """Write a model row, priced from the versioned list. Returns its output_ref."""
        ref = self.ref(output)
        self.call(
            ToolCallRecord(
                kind=kind,
                by="model",
                event_type=AuditEventType.MODEL_CALL,
                summary=summary,
                output_ref=ref,
                result_summary=f"pricing={_pricing_version()}",
                model_id=model_id,
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                retry_count=retry_count,
                basis=basis,
            )
        )
        return ref

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


def invoice_max_age_days() -> int:
    """How old an invoice may be, from the versioned guardrails config.

    Read here rather than held as a constant, so the number a run applied is the
    one named by the ``config_version`` its audit rows carry. A module constant
    would be a policy value that no trail could account for.
    """
    return load_guardrails().tolerances.invoice_max_age_days


def guardrails_version() -> str:
    """The ruleset this run is deciding under.

    Asked of the config rather than passed in, so the string stamped on a
    ``MatchResult`` is the one the loaded file declares. A version supplied by
    the caller could name a file that was not the one consulted, which is the
    single thing a ``config_version`` exists to make impossible.
    """
    return load_guardrails().config_version


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


def decide(record: InvoiceRecord) -> Action:  # noqa: PLR0911 - one arm per state
    """Choose the next step. Calls nothing, reads only the record.

    The default arm is the important one: any state whose tool is unwritten gets
    a stub rather than an error, so the pipeline's shape can be exercised before
    its parts exist. What stops a stub from walking through a human decision is
    the transition table, not this function.
    """
    match record.state:
        case InvoiceState.RECEIVED:
            # One call and a decision on it: what the file is, then whether a
            # model may read it at all.
            return Action(
                kind=ActionKind.INGEST_DOCUMENT, by="rule", rule_id=INPUT_VALIDATE_RULE_ID
            )
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
        case InvoiceState.VENDOR_RESOLVED:
            return Action(kind=ActionKind.FIND_DUPLICATES, by="rule", rule_id=DEDUP_RULE_ID)
        case InvoiceState.CODED:
            return Action(kind=ActionKind.REQUEST_APPROVAL, by="rule", rule_id=APPROVAL_RULE_ID)
        case InvoiceState.PENDING_APPROVAL:
            # Waits for people. Each resume checks whether the snapshot they
            # are approving has gone stale while they waited.
            return Action(
                kind=ActionKind.APPROVAL_REFRESH, by="rule", rule_id=APPROVAL_REFRESH_RULE_ID
            )
        case InvoiceState.EXCEPTION:
            # The one way out of EXCEPTION. The seat explains; the rule routes
            # to a person whatever the seat says, or fails to say.
            return Action(kind=ActionKind.CLASSIFY_EXCEPTION, by="rule", rule_id=CLASSIFY_RULE_ID)
        case InvoiceState.DUPLICATE_CHECKED:
            # One step, up to two calls: the order and what arrived against it.
            # The rule decides whether to ask at all, and what to do with the
            # answers - neither tool decides anything.
            return Action(kind=ActionKind.MATCH_PREP, by="rule", rule_id=MATCH_RULE_ID)
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
        earliest = received - timedelta(days=invoice_max_age_days())
        # Every candidate has to fail. While the date is open nobody has chosen
        # between the readings, so one impossible reading proves nothing - it may
        # simply be the reading that is wrong.
        if all(candidate < earliest for candidate in candidates):
            flags.append(STALE_DATE_FLAG)

    event = InvoiceEvent.VALIDATE if not flags else InvoiceEvent.VALIDATION_FAILED
    return flags, event.value


def apply(  # noqa: PLR0911 - one arm per action kind
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
            return _ingest_step(record, ctx, trail)

        case ActionKind.COMPUTE_EXTRACTION_CONFIDENCE:
            return _read_and_score(record, ctx, trail)

        case ActionKind.VALIDATE:
            return _validate_step(record, ctx, trail)

        case ActionKind.LOOKUP_VENDOR:
            return _resolve_vendor_step(record, ctx, trail)

        case ActionKind.MATCH_PREP:
            return _match_prep_step(record, ctx, trail)

        case ActionKind.CLASSIFY_EXCEPTION:
            return _classify_step(record, ctx, trail)

        case ActionKind.FIND_DUPLICATES:
            return _dedup_step(record, ctx, trail)

        case ActionKind.REQUEST_APPROVAL:
            return _request_approval_step(record, ctx, trail)

        case ActionKind.APPROVAL_REFRESH:
            return _approval_refresh_step(record, ctx, trail)

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


def _ingest_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Find out what the file is, then decide whether a model may read it.

    ``ingest_document`` measures and flags; this rule decides. Any flag sends the
    document to a person *before* the extraction step, which is the only step
    that spends tokens - so a flagged document costs nothing, and a hidden
    instruction in it is never put in front of a model that might act on it.

    Every document gets the decision row, clean ones included. ``flags=none`` is
    a rule that ran and found nothing, which is a different fact from a rule
    that did not run.
    """
    output = _call(trail, ActionKind.INGEST_DOCUMENT, "rule", ctx.ingest)(
        IngestDocumentInput(path=record.source_path)
    )
    summary = f"sha256={output.sha256[:12]}, pages={output.page_count}"
    trail.call(
        ToolCallRecord(
            kind=ActionKind.INGEST_DOCUMENT,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="flagged" if output.flags else "ingested",
            output_ref=trail.ref(output),
            result_summary=summary,
        )
    )
    event = InvoiceEvent.INPUT_FLAGGED if output.flags else InvoiceEvent.INGEST
    return (
        record.model_copy(update={"ingest": output}),
        StepResult(
            event=event.value,
            output_ref=trail.ref(output),
            result_summary=summary,
            decision_basis=_input_basis(output),
        ),
    )


def _input_basis(output: IngestDocumentOutput) -> str:
    """The flags, and the ruleset that defined them, for one audit row.

    ``detail`` is this system's own measurement - page, count, colour, ink -
    and never document text, so nothing planted in the file reaches the trail.
    """
    flags = "|".join(f"{flag.check.value}({flag.detail})" for flag in output.flags)
    return f"config_version={output.config_version}, flags={flags or 'none'}"[:MAX_BASIS_CHARS]


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

    Each reading is put through the output filter the moment it returns, before
    anything else reads it. A flag on either sends the document to a person and
    the readings go no further: not onto the record, not into the confidence
    check. Both readings still run, because they are independent evidence and
    the second one's flags are worth having on the trail too.
    """
    vision = _call(trail, ActionKind.EXTRACT_INVOICE_VISION, "model", ctx.extract)(
        ExtractInvoiceVisionInput(path=record.source_path)
    )
    vision_ref = trail.model_call(
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
    flagged = _screen_reading(trail, ActionKind.EXTRACT_INVOICE_VISION, vision.extraction)

    text = _call(trail, ActionKind.EXTRACT_INVOICE_TEXT, "model", ctx.extract_text)(
        ExtractInvoiceTextInput(path=record.source_path)
    )
    text_ref = trail.model_call(
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
    if text.second_read is not None:
        flagged += _screen_reading(trail, ActionKind.EXTRACT_INVOICE_TEXT, text.second_read)

    if flagged:
        # The readings stay off the record. Nothing downstream may read a
        # flagged transcription; the person this goes to supplies their own.
        return record, StepResult(
            event=InvoiceEvent.OUTPUT_FLAGGED.value,
            output_ref=trail.ref("|".join(flagged)),
            result_summary=f"flags={len(flagged)}",
            decision_basis=(f"config_version={guardrails_version()}, flags={'|'.join(flagged)}")[
                :MAX_BASIS_CHARS
            ],
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
            output_ref=trail.ref(verdict),
            result_summary=f"fields={len(verdict.fields)}, needs_human={len(verdict.needs_human)}",
        )
    )

    if ctx.store is not None:
        # Which readings, by payload ref, and what the check made of them. The
        # text seat's ref is kept only when it actually read something.
        ctx.store.record_extraction(
            str(record.invoice_id),
            vision_ref=vision_ref,
            text_ref=text_ref if text.second_read is not None else None,
            confidence_ref=trail.ref(verdict),
            auto_ok=verdict.auto_ok,
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
            output_ref=trail.ref(vision.extraction),
            result_summary=f"invoice={vision.extraction.invoice_number}",
            decision_basis=confidence_basis(verdict),
        ),
    )


def _screen_reading(trail: StepTrail, kind: ActionKind, extraction: InvoiceExtraction) -> list[str]:
    """Run ``OUTPUT-FILTER@v1`` over one reading and write its row. Returns the hits.

    One row per reading, clean ones included: ``hits=none`` says the filter ran
    and found nothing, which is a different fact from a filter that never ran.
    The row is a rule evaluation filed under the reading's own kind, so the
    trail shows which seat's output was screened. Hits name the pattern and the
    field - ``iban(payment_terms)`` - and never the text that matched.
    """
    config = load_guardrails()
    flags = screen_extraction(extraction, config.output_filter)
    seat = "vision" if kind is ActionKind.EXTRACT_INVOICE_VISION else "text"
    hits = [f"{seat}:{flag.check}({flag.field})" for flag in flags]
    basis = (
        f"config_version={config.config_version}, reading={seat}, hits={'|'.join(hits) or 'none'}"
    )
    trail.call(
        ToolCallRecord(
            kind=kind,
            by="rule",
            rule_id=OUTPUT_FILTER_RULE_ID,
            event_type=AuditEventType.RULE_EVALUATION,
            summary="flagged" if flags else "clean",
            output_ref=trail.ref(basis),
            result_summary=f"hits={len(flags)}",
            basis=basis[:MAX_BASIS_CHARS],
        )
    )
    return hits


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
    chosen = resolve_date_by_receipt_window(candidates, received, invoice_max_age_days())
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
            output_ref=trail.ref(chosen.isoformat()),
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
            # Compared server-side to the master's fingerprint; only a boolean
            # comes back, and nothing here or in the tool echoes the block.
            remit_to_display=extraction.remit_to_display,
        )
    )
    match = found.match
    trail.call(
        ToolCallRecord(
            kind=ActionKind.LOOKUP_VENDOR,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary=match.match_basis.value[:64],
            output_ref=trail.ref(match),
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
                output_ref=trail.ref(match),
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
    if match.remit_to_matches_master is False:
        # A known supplier asking to be paid somewhere new. Held for a person
        # before anything else is decided about the invoice; see the edge.
        return resolved, StepResult(
            event=InvoiceEvent.REMIT_TO_MISMATCH.value,
            output_ref=trail.ref(match),
            result_summary=f"vendor_id={match.vendor_id}",
            decision_basis=_vendor_basis(extraction.vendor_name, match),
        )
    resolved = _settle_by_vendor_locale(resolved, trail)

    return (
        resolved,
        StepResult(
            event=InvoiceEvent.RESOLVE_VENDOR.value,
            output_ref=trail.ref(match),
            result_summary=f"vendor_id={match.vendor_id}, country={match.country}",
            decision_basis=_vendor_basis(extraction.vendor_name, match),
        ),
    )


def _vendor_basis(printed_name: str, match: VendorMatch) -> str:
    """Say what was searched for and what came back, in one line."""
    parts = [f"printed={printed_name!r}", f"basis={match.match_basis.value}"]
    if match.resolved:
        parts.append(f"vendor_id={match.vendor_id}, country={match.country}")
        remit = match.remit_to_matches_master
        parts.append(
            "remit_to=" + ("not_compared" if remit is None else "match" if remit else "mismatch")
        )
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
            output_ref=trail.ref(chosen.isoformat()),
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


def _dedup_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Hold a possible duplicate for a person, then index this invoice.

    ``DEDUP@v1``. The candidates are this vendor's earlier invoices from
    ``dedup_index``; ``find_duplicates`` applies the rule; a match routes to
    ON_HOLD_DUPLICATE and the decision row names the rule and the earlier
    invoice. Either way, this invoice's own entry is added *after* the check -
    so it can never be its own duplicate, and a step restarted after the entry
    was written still sees only the others.

    An invoice that somehow reaches here without a vendor or an extraction is
    held rather than waved through: a check that could not run is not a pass.
    """
    rules = load_guardrails().duplicate_detection
    extraction = record.extraction
    if extraction is None or record.vendor_id is None:
        return record, StepResult(
            event=InvoiceEvent.DUPLICATE_SUSPECTED.value,
            decision_basis="not_checked: no extraction or no resolved vendor",
        )

    printed = extraction.invoice_number
    entry = DedupEntry(
        invoice_id=str(record.invoice_id),
        vendor_id=record.vendor_id,
        normalised_invoice_number=(normalise_invoice_number(printed) or printed.strip())[:64],
        amount=extraction.total,
        currency=extraction.currency,
        invoice_date=effective_date(record),
    )
    window = max(rules.date_window_days, rules.same_amount_window_days)
    candidates = ctx.dedup.candidates(entry, window)
    out = _call(trail, ActionKind.FIND_DUPLICATES, "rule", ctx.find_duplicates)(
        FindDuplicatesInput(entry=entry, candidates=candidates, rules=rules)
    )
    basis = _dedup_basis(out)
    trail.call(
        ToolCallRecord(
            kind=ActionKind.FIND_DUPLICATES,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="suspected" if out.rule else "clear",
            output_ref=trail.ref(out),
            result_summary=f"candidates={out.candidates_compared}",
            basis=basis,
        )
    )
    # After the check, never before: an invoice must not be its own duplicate.
    ctx.dedup.add(entry)

    event = InvoiceEvent.DUPLICATE_SUSPECTED if out.rule else InvoiceEvent.CHECK_DUPLICATES
    record = record.model_copy(
        update={
            "duplicate_of": out.candidate_invoice_id,
            "duplicate_rule": out.rule.value if out.rule else None,
        }
    )
    return record, StepResult(
        event=event.value,
        output_ref=trail.ref(out),
        result_summary=f"candidates={out.candidates_compared}",
        decision_basis=basis,
    )


def _dedup_basis(out: FindDuplicatesOutput) -> str:
    """Which rule fired against which earlier invoice - the queue item's whole case."""
    if out.rule is None:
        return f"rule=none, candidates={out.candidates_compared}"
    return (
        f"rule={out.rule.value}, candidate={out.candidate_invoice_id}, "
        f"similarity={out.similarity}, candidates={out.candidates_compared}"
    )[:MAX_BASIS_CHARS]


def _request_approval_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """``APPROVAL-MATRIX@v1``: work out who must approve, open the request, tell them.

    The requirement is computed from the matrix and the invoice's facts - its
    amount and currency, whether the vendor resolved, whether this is the
    vendor's first invoice, a remit-to mismatch, an exception a person accepted
    - and recorded as an ``approvals`` row by ``request_approval``. ``notify``
    tells the role. The decision row's basis is the matrix's working, with the
    rule id and the amount compared.

    The snapshot the approvers will be signing off - order, receipts, match -
    is stamped now, so APPROVAL-REFRESH@v1 can tell later whether it has gone
    stale while they waited.

    Whether this is the vendor's first invoice needs the store; without one
    (tests) the answer is "unknown", treated as yes - the conservative reading.
    """
    config = load_guardrails()
    extraction = record.extraction
    if extraction is None:  # unreachable on the real path; held rather than raised
        return record, StepResult(
            event=InvoiceEvent.REQUEST_APPROVAL.value, decision_basis="no extraction to approve"
        )
    vendor_id = record.vendor_id
    first_invoice = (
        ctx.store.prior_invoice_count(vendor_id, excluding=str(record.invoice_id)) == 0
        if ctx.store is not None and vendor_id is not None
        else True
    )
    requirement = approval_requirement(
        config.approval_matrix,
        config_version=config.config_version,
        amount=extraction.total,
        currency=extraction.currency,
        vendor_resolved=vendor_id is not None,
        first_invoice_from_vendor=first_invoice,
        remit_to_mismatch=bool(
            record.vendor_match and record.vendor_match.remit_to_matches_master is False
        ),
        match_exception=bool(record.match_result and record.match_result.reason_codes),
    )

    key = f"{record.invoice_id}:approval:{record.approval_round}"
    token = CURRENT_APPROVAL_STORE.set(ctx.approvals)
    try:
        opened = _call(trail, ActionKind.REQUEST_APPROVAL, "rule", ctx.request_approval)(
            RequestApprovalInput(
                idempotency_key=key,
                invoice_id=str(record.invoice_id),
                requirement=requirement,
                config_version=config.config_version,
            )
        )
    finally:
        CURRENT_APPROVAL_STORE.reset(token)
    trail.call(
        ToolCallRecord(
            kind=ActionKind.REQUEST_APPROVAL,
            by="rule",
            event_type=AuditEventType.APPROVAL_REQUESTED,
            summary="already_requested" if opened.already_existed else "requested",
            output_ref=trail.ref(opened),
            result_summary=f"approval_id={opened.approval_id}",
            basis=(
                f"approval_id={opened.approval_id}, required={opened.required_approvers}, "
                f"role={opened.approver_role}"
            ),
        )
    )

    told = _call(trail, ActionKind.NOTIFY, "rule", ctx.notify)(
        NotifyInput(
            idempotency_key=f"{key}:notify",
            recipient_user_id=requirement.approver_role,
            template_id="approval_requested",
            template_vars={
                "approval_id": opened.approval_id,
                "required": str(requirement.required_approvers),
                "amount": f"{requirement.amount} {requirement.currency}",
            },
            invoice_id=str(record.invoice_id),
        )
    )
    trail.call(
        ToolCallRecord(
            kind=ActionKind.NOTIFY,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="delivered" if told.delivered else "not_delivered",
            output_ref=trail.ref(told),
            result_summary=f"channel={told.channel}, recipient={requirement.approver_role}",
        )
    )

    return (
        record.model_copy(
            update={"approval_id": opened.approval_id, "snapshot_taken_at": ctx.now()}
        ),
        StepResult(
            event=InvoiceEvent.REQUEST_APPROVAL.value,
            output_ref=trail.ref(requirement),
            result_summary=f"approval_id={opened.approval_id}",
            decision_basis=requirement.basis,
        ),
    )


def _approval_refresh_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """``APPROVAL-REFRESH@v1``: is the snapshot the approvers are signing still true?

    Fresh enough, and the invoice simply goes on waiting - the row says how many
    approvals it has and when the snapshot will be checked again. Older than
    ``pending_approval_refresh_after_hours``: the order and receipts are fetched
    again and the match re-run.

    * **Still holds** - no reason code the approvers did not already see. The
      fresher snapshot replaces the old one and the invoice keeps waiting.
    * **No longer holds** - a new reason code, or the order gone. The request is
      cancelled (people cannot approve numbers that are no longer true) and the
      invoice goes to EXCEPTION, where CLASSIFY@v1 hands it back to a person.
    """
    matrix = load_guardrails().approval_matrix
    threshold = timedelta(hours=matrix.pending_approval_refresh_after_hours)
    view = ctx.approvals.get(record.approval_id) if record.approval_id else None
    progress = (
        f"approval_id={record.approval_id}, approvals={len(view.approvals)} of "
        f"{view.required_approvers}"
        if view
        else "approval_id=none"
    )
    taken = record.snapshot_taken_at
    age = ctx.now() - taken if taken is not None else None
    order = record.purchase_order

    if age is None or age < threshold or order is None:
        hours = f"{age.total_seconds() / 3600:.1f}h" if age is not None else "unknown"
        why = (
            "no purchase order to refresh"
            if order is None and age is not None and age >= threshold
            else f"snapshot age {hours} < {matrix.pending_approval_refresh_after_hours}h"
        )
        return record, StepResult(
            event=InvoiceEvent.STUB_OK.value,
            decision_basis=f"rule={APPROVAL_REFRESH_RULE_ID}, awaiting approval, {progress}, {why}",
        )

    fresh_order, received, result = _refetch(record, ctx, trail, order.po_number)
    old_codes: set[ReasonCode] = (
        set(record.match_result.reason_codes) if record.match_result else set()
    )
    new_codes = set(result.reason_codes) if result is not None else {ReasonCode.PO_NOT_FOUND}
    added = sorted(code.value for code in new_codes - old_codes)
    hours = f"{age.total_seconds() / 3600:.1f}h"
    if not added:
        return (
            record.model_copy(
                update={
                    "purchase_order": fresh_order,
                    "receipts": received,
                    "match_result": result,
                    "snapshot_taken_at": ctx.now(),
                }
            ),
            StepResult(
                event=InvoiceEvent.STUB_OK.value,
                output_ref=trail.ref(result) if result else None,
                decision_basis=(
                    f"rule={APPROVAL_REFRESH_RULE_ID}, snapshot age {hours} refreshed, "
                    f"still holds, awaiting approval, {progress}"
                ),
            ),
        )

    if record.approval_id:
        ctx.approvals.cancel(
            record.approval_id,
            by=f"rule:{APPROVAL_REFRESH_RULE_ID}",
            reason=f"match changed: {'|'.join(added)}",
        )
    return (
        record.model_copy(
            update={
                "purchase_order": fresh_order,
                "receipts": received,
                "match_result": result,
                "approval_id": None,
                "approval_round": record.approval_round + 1,
                "snapshot_taken_at": None,
            }
        ),
        StepResult(
            event=InvoiceEvent.MATCH_CHANGED.value,
            output_ref=trail.ref(result) if result else None,
            decision_basis=(
                f"rule={APPROVAL_REFRESH_RULE_ID}, snapshot age {hours}, match changed: "
                f"new={'|'.join(added)}, approval {record.approval_id} cancelled, "
                f"back to a person"
            ),
        ),
    )


def _refetch(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail, po_number: str
) -> tuple[PurchaseOrder | None, ReceiptSet | None, MatchResult | None]:
    """Fetch the order and its receipts again and re-run the match, one row per call.

    The order alone when it is the only thing that can be fetched; no match
    without an order and an extraction.
    """
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
            output_ref=trail.ref(order) if order else trail.ref(f"po_not_found:{po_number}"),
            result_summary=f"po_number={po_number}",
            latency_ms=fetched.latency_ms,
        )
    )
    if order is None or record.extraction is None:
        return order, None, None

    got = _call(trail, ActionKind.GET_RECEIPTS, "rule", ctx.get_receipts)(
        GetReceiptsInput(po_number=po_number)
    )
    receipts = got.receipts
    trail.call(
        ToolCallRecord(
            kind=ActionKind.GET_RECEIPTS,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="nothing_received" if receipts.is_empty else "received",
            output_ref=trail.ref(receipts),
            result_summary=f"lines={len(receipts.lines)}",
            latency_ms=got.latency_ms,
        )
    )
    matched = _call(trail, ActionKind.COMPUTE_MATCH, "rule", ctx.compute_match)(
        ComputeMatchInput(
            invoice_id=str(record.invoice_id),
            extraction=record.extraction,
            purchase_orders=[order],
            receipts=[receipts],
            config_version=guardrails_version(),
        )
    )
    result = matched.result
    trail.call(
        ToolCallRecord(
            kind=ActionKind.COMPUTE_MATCH,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="matched" if result.matched else "exception",
            output_ref=trail.ref(result),
            result_summary=f"po_number={po_number}, lines={len(result.lines)}",
        )
    )
    return order, receipts, result


def _match_prep_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Gather what the matcher compares, refuse it anything unsafe, then match.

    Four outcomes and no fifth:

    * **No PO reference on the document** - the invoice goes down the NON_PO
      path, which is a different pipeline with its own coding and approval, not
      a failed match. Nothing is fetched: there is nothing to fetch.
    * **The order is missing, or belongs to someone else** - an exception, and
      the matcher never sees it. The second of those is an identity check rather
      than a tolerance: an invoice quoting another supplier's PO number is a
      fraud signal, and a signal that gets compared against a band is a signal
      that can be argued into passing.
    * **The order is this vendor's and the match is clean** - MATCHED.
    * **The order is this vendor's and the match is not** - EXCEPTION, with the
      reason codes and the config version in the decision's own audit row.

    The identity checks stay *here* rather than moving into ``compute_match``,
    and that boundary is the point of this step. Whether this order belongs to
    this vendor is a comparison of ERP ids, which the matcher is deliberately
    not given - it gets the numbers and nothing that would let it decide who
    anyone is. So the loop answers "may these two be compared at all" and the
    matcher answers "and do they agree".
    """
    extraction = record.extraction
    if extraction is None or not extraction.po_references:
        return record, StepResult(
            event=InvoiceEvent.NO_PO_REFERENCE.value,
            decision_basis="po_references=none",
        )
    references = list(extraction.po_references)

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
            output_ref=trail.ref(order) if order else trail.ref(f"po_not_found:{po_number}"),
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
                output_ref=trail.ref(f"po_not_found:{po_number}"),
                result_summary=f"po_number={po_number}",
                decision_basis=f"po_number={po_number}, po_not_found",
            ),
        )

    mismatch = _vendor_mismatch(record, order)
    if mismatch is not None:
        return (
            record.model_copy(update={"purchase_order": order, "match_result": mismatch}),
            StepResult(
                event=InvoiceEvent.MATCH_EXCEPTION.value,
                output_ref=trail.ref(mismatch),
                result_summary=f"po_number={po_number}",
                decision_basis=_match_basis(mismatch, order, None),
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
            output_ref=trail.ref(received.receipts),
            result_summary=f"lines={len(received.receipts.lines)}",
            basis=f"po_number={po_number}, lines={len(received.receipts.lines)}",
            latency_ms=received.latency_ms,
        )
    )

    matched = _call(trail, ActionKind.COMPUTE_MATCH, "rule", ctx.compute_match)(
        ComputeMatchInput(
            invoice_id=str(record.invoice_id),
            extraction=extraction,
            purchase_orders=[order],
            receipts=[received.receipts],
            config_version=guardrails_version(),
        )
    )
    result = matched.result
    trail.call(
        ToolCallRecord(
            kind=ActionKind.COMPUTE_MATCH,
            by="rule",
            event_type=AuditEventType.TOOL_CALL,
            summary="matched" if result.matched else "exception",
            output_ref=trail.ref(result),
            result_summary=f"po_number={po_number}, lines={len(result.lines)}",
            basis=_match_prep_basis(order, received.receipts),
        )
    )

    return (
        record.model_copy(
            update={
                "purchase_order": order,
                "receipts": received.receipts,
                "match_result": result,
            }
        ),
        StepResult(
            event=(InvoiceEvent.MATCH if result.matched else InvoiceEvent.MATCH_EXCEPTION).value,
            output_ref=trail.ref(result),
            result_summary=f"po_number={po_number}",
            decision_basis=_match_basis(result, order, received.receipts),
        ),
    )


def _vendor_mismatch(record: InvoiceRecord, order: PurchaseOrder) -> MatchResult | None:
    """Return a result refusing this order as another supplier's, or None.

    An identity check, not a tolerance. A tolerance answers "is this close
    enough", and there is no close enough to belonging to a different supplier -
    an invoice quoting another vendor's purchase-order number is one of the
    oldest frauds in accounts payable, and a signal compared against a band is a
    signal that can be argued into passing.

    Compares ERP ids, never names. The name is what the document claimed, and
    the claim is the thing under suspicion.

    It returns a ``MatchResult`` rather than the sentence it used to return, so
    that an invoice held for identity carries the same shape of evidence as one
    held for a price: a code from the closed vocabulary, on the record, that a
    downstream reader can branch on. A prose basis was readable and unmatchable -
    the explanation seat would have had to parse English to find out what
    happened, which is the sort of thing that works until somebody rewords it.

    No lines on it. Nothing was compared, and rows implying otherwise would be a
    line-by-line breakdown of an order this invoice has no claim on.
    """
    if record.vendor_id is None or order.vendor_erp_id == record.vendor_id:
        return None
    return MatchResult(
        matched=False,
        reason_codes=[ReasonCode.PO_VENDOR_MISMATCH],
        config_version=guardrails_version(),
        po_number=order.po_number,
    )


def _match_prep_basis(order: PurchaseOrder, receipts: ReceiptSet) -> str:
    """One line saying what was gathered, for the matcher's own audit row."""
    return (
        f"po_number={order.po_number}, po_lines={len(order.lines)}, "
        f"receipt_lines={len(receipts.lines)}, status={order.status.value}"
    )[:MAX_BASIS_CHARS]


def _match_basis(result: MatchResult, order: PurchaseOrder, receipts: ReceiptSet | None) -> str:
    """Why this invoice matched or did not, in one line of the audit trail.

    The reason codes and the config version are both on it, and both have to be:
    a code without the ruleset that raised it cannot be re-checked next quarter,
    because the tolerance behind it may have moved. Naming the version is what
    makes "why was this held" answerable from the row rather than from a guess
    about which file was in force.
    """
    parts = [
        f"po_number={order.po_number}",
        f"po_lines={len(order.lines)}",
        f"receipt_lines={len(receipts.lines) if receipts is not None else 0}",
        f"status={order.status.value}",
        f"config_version={result.config_version}",
        "matched=" + ("yes" if result.matched else "no"),
    ]
    if result.reason_codes:
        parts.append("reasons=" + "|".join(code.value for code in result.reason_codes))
    if receipts is not None:
        # Last, so the slice below cuts line detail rather than the codes.
        parts.append(_match_lines_summary(result))
    return ", ".join(parts)[:MAX_BASIS_CHARS]


def _match_lines_summary(result: MatchResult) -> str:
    """Each paired line's verdict and the numbers behind it, compactly.

    ``lines=1:ok,2:price_over(3.00%,642.00;limit 2%/50) ; unmatched=0 ; unbilled=0``

    So a reviewer reading the trail sees *which* line raised a code and by how
    much, without opening the stored result. Lines are named by purchase-order
    line number, never by description: a description is vendor-controlled text,
    and none of it goes in the trail.
    """
    tolerances = load_guardrails().tolerances
    paired: list[str] = []
    unmatched = unbilled = 0
    for line in result.lines:
        if line.outcome is MatchLineOutcome.UNMATCHED:
            unmatched += 1
        elif line.outcome is MatchLineOutcome.UNBILLED:
            unbilled += 1
        elif line.outcome is MatchLineOutcome.PRICE_OVER:
            pct = "n/a" if line.price_variance_pct is None else f"{line.price_variance_pct:.2f}%"
            paired.append(
                f"{line.line_no}:price_over({pct},{line.price_variance_abs};"
                f"limit {_plain(tolerances.price_variance_pct)}%/"
                f"{_plain(tolerances.price_variance_abs)})"
            )
        elif line.outcome is MatchLineOutcome.QTY_OVER:
            billed = "none" if line.invoice_qty is None else _plain(line.invoice_qty)
            received = "none" if line.received_qty is None else _plain(line.received_qty)
            paired.append(
                f"{line.line_no}:qty_over({billed}>{received};"
                f"limit {_plain(tolerances.qty_over_billing_pct)}%)"
            )
        else:
            paired.append(f"{line.line_no}:{line.outcome.value}")
    return f"lines={','.join(paired) or 'none'} ; unmatched={unmatched} ; unbilled={unbilled}"


def _plain(value: Decimal) -> str:
    """A decimal without trailing zeros or an exponent: ``2.0`` -> ``2``."""
    return f"{value.normalize():f}"


def _classify_step(
    record: InvoiceRecord, ctx: RunContext, trail: StepTrail
) -> tuple[InvoiceRecord, StepResult]:
    """Explain the exception, then hand it to a person - always.

    ``CLASSIFY@v1`` moves EXCEPTION to PENDING_HUMAN on every path, and that is
    the point of putting the seat here rather than on the route: the reason
    codes already decided that a person must look, and the explanation only
    makes the look quicker. So the move does not wait on the model:

    * **Accepted** - the classification goes on the record, and the decision
      row carries the lead reason, the resolver and the action.
    * **Rejected by the checks in code** - the model row records why; the
      record keeps no classification; the person sees the codes.
    * **Failed** - an API error, refusal or truncation. The seat's own failure
      row is already on the trail; the invoice moves on without prose.
    * **Nothing to explain** - no match result, or one with no codes (a PO that
      was never found, or an exception reached from posting). No call is made.

    The seat is given codes, numbers and ids built by the snapshot contracts'
    ``from_...`` constructors, which drop every string a vendor wrote.
    """
    config = load_guardrails()
    result = record.match_result
    extraction = record.extraction
    if result is None or not result.reason_codes or extraction is None:
        why = "not_run: no reason codes to explain"
        return (
            record.model_copy(update={"classification": None, "classification_rejected": why}),
            StepResult(
                event=InvoiceEvent.CLASSIFY.value,
                decision_basis=_classify_basis(config.config_version, result, None, why),
            ),
        )

    payload = ClassifyExceptionInput(
        match_result=result,
        header_numbers=HeaderNumbers.from_extraction(extraction),
        po_snapshot=(
            PoSnapshot.from_purchase_order(record.purchase_order) if record.purchase_order else None
        ),
        vendor_summary=(
            VendorSummary.from_match(record.vendor_match) if record.vendor_match else None
        ),
        config=config,
    )
    try:
        out = _call(trail, ActionKind.CLASSIFY_EXCEPTION, "model", ctx.classify)(payload)
    except ClassificationError as exc:
        # The seat's own failure row is already written; this one routes.
        why = f"failed: {type(exc).__name__}"
        return (
            record.model_copy(update={"classification": None, "classification_rejected": why}),
            StepResult(
                event=InvoiceEvent.CLASSIFY.value,
                decision_basis=_classify_basis(config.config_version, result, None, why),
            ),
        )

    trail.model_call(
        ActionKind.CLASSIFY_EXCEPTION,
        out.classification if out.classification is not None else out,
        summary="classified" if out.classification is not None else "rejected",
        model_id=out.model_id,
        prompt_version=out.prompt_version,
        input_tokens=out.input_tokens,
        output_tokens=out.output_tokens,
        latency_ms=out.latency_ms,
        retry_count=out.retry_count,
        basis=f"rejected: {out.rejected}" if out.rejected else None,
    )
    why = f"rejected: {out.rejected}" if out.rejected else None
    return (
        record.model_copy(
            update={"classification": out.classification, "classification_rejected": why}
        ),
        StepResult(
            event=InvoiceEvent.CLASSIFY.value,
            output_ref=trail.ref(out.classification) if out.classification else None,
            result_summary=f"codes={len(result.reason_codes)}",
            decision_basis=_classify_basis(config.config_version, result, out.classification, why),
        ),
    )


def _classify_basis(
    config_version: str,
    result: MatchResult | None,
    classification: ExceptionClassification | None,
    why: str | None,
) -> str:
    """The decision row: what the rules found, and how it was handed to a person.

    Resolver and action are on the row because they are what the queue routes
    on. The summary is not: it is prose, it is on the record, and the trail
    keeps its hash rather than its words.
    """
    codes = "|".join(code.value for code in result.reason_codes) if result else ""
    parts = [f"config_version={config_version}", f"codes={codes or 'none'}"]
    if classification is not None:
        parts += [
            f"lead={classification.reason_code.value}",
            f"resolver={classification.suggested_resolver.value}",
            f"action={classification.suggested_action.value}",
        ]
    else:
        parts += ["resolver=none", "action=none", f"classification={why}"]
    return ", ".join(parts)[:MAX_BASIS_CHARS]


_HUMAN_DOORS: dict[InvoiceState, frozenset[InvoiceEvent]] = {
    InvoiceState.PENDING_HUMAN: frozenset(
        {
            InvoiceEvent.REMATCH,
            InvoiceEvent.ACCEPT_WITH_REASON,
            InvoiceEvent.REJECT,
            InvoiceEvent.CANCEL,
        }
    ),
    InvoiceState.ON_HOLD_DUPLICATE: frozenset(
        {InvoiceEvent.CONFIRM_DUPLICATE, InvoiceEvent.NOT_DUPLICATE, InvoiceEvent.CANCEL}
    ),
    InvoiceState.PENDING_APPROVAL: frozenset(
        {InvoiceEvent.APPROVE, InvoiceEvent.REJECT, InvoiceEvent.CANCEL}
    ),
}
"""Which decisions a person may make in which waiting state."""

_REASON_REQUIRED: frozenset[InvoiceEvent] = frozenset(
    {InvoiceEvent.ACCEPT_WITH_REASON, InvoiceEvent.NOT_DUPLICATE, InvoiceEvent.APPROVE}
)
"""Overriding a rule, clearing a duplicate, approving a payment: each needs words."""


def apply_human_decision(  # noqa: PLR0912, PLR0915 - one branch per door
    record: InvoiceRecord,
    ctx: RunContext,
    *,
    event: InvoiceEvent,
    user_id: str,
    reason: str | None = None,
) -> InvoiceRecord:
    """Apply a person's decision on a waiting invoice, and write the row that says so.

    The only way to fire a ``HUMAN_ONLY_EVENTS`` event; the loop refuses them.
    Three doors:

    * **PENDING_HUMAN** - ``rematch`` (bounded by ``loop_limits.max_rematches``;
      the stale explanation is cleared), ``accept_with_reason`` to MATCHED
      (reason required), ``reject``, ``cancel``.
    * **ON_HOLD_DUPLICATE** - ``confirm_duplicate`` to REJECTED, with the
      earlier invoice's id on the row; ``not_duplicate`` to DUPLICATE_CHECKED,
      reason required, refused for an exact number match (the database holds one
      invoice per number); ``cancel``.
    * **PENDING_APPROVAL** - ``approve`` (reason required) and ``reject``
      (reason required, as every decision row needs one), recorded in
      ``approval_decisions``. The invoice moves only when the request completes:
      the first reject rejects it, enough distinct approvers approve it. A vote
      that does not complete it writes a row and leaves the state alone.
      ``cancel`` withdraws the request.

    The row's actor is the person. Returns the record as it now stands; the
    caller runs the loop again if there is more to do.

    Raises:
        HumanDecisionError: Not a human event, not a door it opens, no user, a
            missing reason, the re-match limit spent, a second vote from one
            person, or an exact duplicate declared not one. Raised before
            anything is written - nothing changed, so there is nothing to record.
    """
    if event not in HUMAN_ONLY_EVENTS:
        msg = f"{event.value} is not a decision a person makes"
        raise HumanDecisionError(msg)
    doors = _HUMAN_DOORS.get(record.state)
    if doors is None:
        msg = (
            "a person acts on PENDING_HUMAN, ON_HOLD_DUPLICATE or PENDING_APPROVAL; "
            f"this invoice is {record.state.value}"
        )
        raise HumanDecisionError(msg)
    if event not in doors:
        msg = f"{event.value} is not a decision on {record.state.value}"
        raise HumanDecisionError(msg)
    user = user_id.strip()
    if not user:
        msg = "a human decision must name who made it"
        raise HumanDecisionError(msg)
    text = (reason or "").strip()
    approving_reject = (
        record.state is InvoiceState.PENDING_APPROVAL and event is InvoiceEvent.REJECT
    )
    if (event in _REASON_REQUIRED or approving_reject) and not text:
        msg = f"{event.value} needs a written reason"
        raise HumanDecisionError(msg)

    config = load_guardrails()
    update: dict[str, object] = {}
    basis = f"reason={text or 'none'}"

    if event is InvoiceEvent.REMATCH:
        limit = config.loop_limits.max_rematches
        if record.rematch_count >= limit:
            msg = f"re-match limit reached: {record.rematch_count} of {limit}"
            raise HumanDecisionError(msg)
        count = record.rematch_count + 1
        update = {"rematch_count": count, "classification": None, "classification_rejected": None}
        basis = f"rematch={count} of {limit}, config_version={config.config_version}"
    elif record.state is InvoiceState.ON_HOLD_DUPLICATE:
        if event is InvoiceEvent.NOT_DUPLICATE and record.duplicate_rule == "exact_number":
            msg = (
                f"same vendor and invoice number as {record.duplicate_of}: the database keeps "
                "one invoice per number, so confirm one of them as the duplicate"
            )
            raise HumanDecisionError(msg)
        basis = (
            f"candidate={record.duplicate_of}, rule={record.duplicate_rule}, "
            f"reason={text or 'none'}"
        )
    elif record.state is InvoiceState.PENDING_APPROVAL:
        if record.approval_id is None:
            msg = "no approval request on this invoice"
            raise HumanDecisionError(msg)
        if event is InvoiceEvent.CANCEL:
            ctx.approvals.cancel(record.approval_id, by=f"user:{user}", reason=text or "cancelled")
        else:
            vote: Literal["approve", "reject"] = (
                "approve" if event is InvoiceEvent.APPROVE else "reject"
            )
            try:
                view = ctx.approvals.decide(
                    record.approval_id, user_id=user, decision=vote, reason=text
                )
            except ApprovalDecisionRefusedError as exc:
                raise HumanDecisionError(str(exc)) from exc
            basis = (
                f"approval_id={record.approval_id}, {vote}, approvals={len(view.approvals)} of "
                f"{view.required_approvers}, status={view.status}, reason={text}"
            )
            if view.status == "pending":
                # Counted, but not enough yet: the row is the record of the vote.
                _write(
                    ctx,
                    record,
                    Action(kind=ActionKind.HUMAN_DECISION, by="human", user_id=user),
                    StepResult(event=event.value, decision_basis=basis[:MAX_BASIS_CHARS]),
                    StepTransition(record.state, None, AuditEventType.HUMAN_DECISION),
                )
                return record

    to_state = transition(record.state, event.value)
    updated = record.model_copy(update=update)
    _write(
        ctx,
        updated,
        Action(kind=ActionKind.HUMAN_DECISION, by="human", user_id=user),
        StepResult(event=event.value, decision_basis=basis[:MAX_BASIS_CHARS]),
        StepTransition(record.state, to_state),
    )
    decided = updated.model_copy(update={"state": to_state})
    if ctx.store is not None:
        ctx.store.save(decided)
    return decided


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
        ActionKind.INGEST_DOCUMENT,
        ActionKind.CLASSIFY_EXCEPTION,
        ActionKind.FIND_DUPLICATES,
        ActionKind.REQUEST_APPROVAL,
        ActionKind.APPROVAL_REFRESH,
        ActionKind.VALIDATE,
        ActionKind.STUB,
        ActionKind.MATCH_PREP,
        ActionKind.COMPUTE_EXTRACTION_CONFIDENCE,
        ActionKind.LOOKUP_VENDOR,
    }
)
"""Kinds whose *step* is a decision over several calls rather than one call.

A step that **is** one call is attributed to the callee: "which tool ran" is what
an auditor wants. ``ingest_document`` used to be such a step; it is in this set
now because its step decides whether a model may read the file at all, and that
decision belongs to ``INPUT-VALIDATE@v1`` rather than to the tool. A
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
        return HumanActor(user_id=action.user_id or "unknown")
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


@dataclass
class _Progress:
    """How far a run got, for its ``runs`` row."""

    steps: int = 0
    last_step: str | None = None


def run(
    record: InvoiceRecord, ctx: RunContext, max_steps: int = DEFAULT_MAX_STEPS
) -> InvoiceRecord:
    """Advance one invoice as far as it can go without a human.

    Stops on any of: a terminal state, a step that raised, a state the table
    will not leave, or the step budget. Every one of those writes an event
    before returning - a run that ends silently is indistinguishable from one
    that never started.

    With a store, the store is the truth: the invoice is reloaded at the top of
    every iteration and saved after every transition, and the run gets a
    ``runs`` row that says how it ended. A run that dies mid-step leaves the
    invoice in the state the step began in, so running it again restarts that
    step and nothing before it.

    Never raises. A failed invoice is a routing outcome, not a crash: the caller
    gets a record whose state says where it stopped.
    """
    progress = _Progress()
    if ctx.store is not None:
        ctx.store.start_run(ctx.run_id, str(record.invoice_id))
    try:
        record = _advance(record, ctx, max_steps, progress)
    finally:
        if ctx.store is not None:
            ctx.store.finish_run(
                ctx.run_id,
                step_count=progress.steps,
                last_step=progress.last_step,
                last_state=record.state,
            )
    return record


def _advance(
    record: InvoiceRecord, ctx: RunContext, max_steps: int, progress: _Progress
) -> InvoiceRecord:
    """The loop body. See :func:`run`."""
    while progress.steps < max_steps:
        if ctx.store is not None:
            # The database, not this process's memory, says where the invoice is.
            record = ctx.store.load(str(record.invoice_id))
        if is_terminal(record.state):
            break

        from_state = record.state
        action = decide(record)
        trail = StepTrail(ctx, record, from_state)

        try:
            record, result = apply(action, record, ctx, trail)
        except Exception as exc:
            # The failing tool has already written its own row through the trail.
            # This one records that the *step* stopped, and why. Nothing is
            # saved: the invoice stays where the step began, so a re-run restarts it.
            result = StepResult(
                event="error",
                error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
                error_class=type(exc).__name__,
            )
            _write(
                ctx, record, action, result, StepTransition(from_state, None, AuditEventType.ERROR)
            )
            progress.last_step = "error"
            log.exception("step_failed", invoice_id=str(record.invoice_id))
            return record

        if result.event in _HUMAN_ONLY_VALUES:
            # A rule or a model emitted a decision only a person may make. That
            # is a defect, not a route - the system must never approve its own
            # exception - so it is recorded as an error and the run stops.
            refused = result.model_copy(
                update={
                    "error": f"{result.event} is human-only; the loop may not fire it",
                    "error_class": "HumanOnlyEvent",
                }
            )
            _write(
                ctx, record, action, refused, StepTransition(from_state, None, AuditEventType.ERROR)
            )
            progress.last_step = "error"
            return record

        try:
            to_state = transition(from_state, result.event)
        except IllegalTransition as exc:
            _write(ctx, record, action, *_halt(from_state, result, exc))
            if ctx.store is not None:
                # The state does not move, but what the step learned is kept -
                # a refreshed approval snapshot, for one.
                ctx.store.save(record)
            progress.last_step = HALT_NO_TOOL
            log.info("awaiting_human", invoice_id=str(record.invoice_id), state=from_state.value)
            return record

        # The row first, then the state. A crash between them repeats the step's
        # row on resume rather than losing it - rule 4 says every transition is
        # recorded, and a duplicate is visible where a gap is not. Piece 4's
        # checkpointing removes the duplicate.
        _write(ctx, record, action, result, StepTransition(from_state, to_state))
        record = record.model_copy(update={"state": to_state})
        if ctx.store is not None:
            ctx.store.save(record)
        progress.steps += 1
        progress.last_step = result.event

    if not is_terminal(record.state):
        _escalate(ctx, record, max_steps)

    return record


_HUMAN_ONLY_VALUES: frozenset[str] = frozenset(event.value for event in HUMAN_ONLY_EVENTS)


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
                    "decision_basis": result.decision_basis
                    or f"no tool for the step after {from_state.value}",
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
