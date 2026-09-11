"""The agent loop, driven entirely offline.

The extraction seat is injected through ``RunContext``, so no test here can
reach the API even if the guard in conftest were removed. That is deliberate:
the loop's job is routing, and routing should be testable without a network,
a database or a key.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.contracts.audit import HumanActor, ModelActor, RuleActor, ToolActor, utc_now
from ap_agent.contracts.enums import ArithmeticFlag, AuditEventType
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.run import Action, ActionKind, InvoiceRecord, StepResult
from ap_agent.loop.runner import (
    DEFAULT_MAX_STEPS,
    LOOP_ONLY_FLAGS,
    NO_SECOND_READ,
    STUB_HOOKS,
    VENDOR_LOCALE_DATE_HOOK,
    RunContext,
    actor_for,
    apply,
    decide,
    run,
    validate_extraction,
)
from ap_agent.states.machine import InvoiceState
from ap_agent.tools import TOOL_MODULE_NAMES
from ap_agent.tools.compute_extraction_confidence import DateResolutionReason, DateVerdict
from ap_agent.tools.extract_invoice_text import ExtractInvoiceTextOutput
from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionOutput
from ap_agent.tools.ingest_document import IngestDocumentInput, IngestDocumentOutput

if TYPE_CHECKING:
    from collections.abc import Callable

    from ap_agent.tools.extract_invoice_text import ExtractInvoiceTextInput
    from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionInput

HAPPY_PATH_STEPS = 14
"""Loop iterations from RECEIVED to CLOSED. One per state change."""

HAPPY_PATH_EVENTS = [
    "ingest",
    # One step, four rows: two readings, the check that scored them, and the
    # decision that moved the invoice. Only the last one carries a to_state.
    "read",
    "read",
    "auto_ok",
    "extract",
    "validate",
    *["stub_ok"] * 11,
]


def _fake_ingest(_payload: IngestDocumentInput) -> IngestDocumentOutput:
    return IngestDocumentOutput(
        sha256="a" * 64,
        byte_size=1234,
        media_type="application/pdf",
        page_count=1,
        has_text_layer=True,
        text_char_count=500,
        pages_with_text=1,
        page_sharpness=[900.0],
    )


def _extraction(**overrides: object) -> InvoiceExtraction:
    payload: dict[str, object] = {
        "vendor_name": "Acme",
        "invoice_number": "INV-1",
        "invoice_date": date(2026, 1, 1),
        "currency": "USD",
        "subtotal": Decimal("100.00"),
        "tax_total": Decimal("20.00"),
        "total": Decimal("120.00"),
        "line_items": [
            {
                "description": "Widget",
                "quantity": Decimal(1),
                "unit": "EA",
                "unit_price": Decimal("100.00"),
                "extended_price": Decimal("100.00"),
            }
        ],
    }
    payload.update(overrides)
    return InvoiceExtraction.model_validate(payload)


def _fake_extract_factory(
    extraction: InvoiceExtraction | None = None,
) -> Callable[[ExtractInvoiceVisionInput], ExtractInvoiceVisionOutput]:
    def _extract(_payload: ExtractInvoiceVisionInput) -> ExtractInvoiceVisionOutput:
        return ExtractInvoiceVisionOutput(
            extraction=extraction or _extraction(),
            model_id="claude-sonnet-5",
            prompt_version="extract_v1",
            input_tokens=7000,
            output_tokens=900,
            latency_ms=8000,
        )

    return _extract


def _page_text(extraction: InvoiceExtraction, date_rendering: str | None = None) -> str:
    """A text layer that actually contains what the extraction claims.

    The loop runs the *real* confidence check - it is pure code, so there is no
    reason to fake it - which means a fake text layer has to ground the values or
    every test invoice would be routed to a human. Dates render ISO by default,
    which is unambiguous; pass ``date_rendering`` for the slash-date cases.
    """
    rendering = date_rendering or extraction.invoice_date.isoformat()
    return "\n".join(
        [
            extraction.vendor_name,
            f"Invoice No: {extraction.invoice_number}",
            f"Date of issue: {rendering}",
            f"Subtotal {extraction.subtotal}",
            f"Tax {extraction.tax_total}",
            f"Total {extraction.total} {extraction.currency}",
        ]
    )


def _fake_text_factory(
    extraction: InvoiceExtraction | None = None,
    *,
    second_read: InvoiceExtraction | None = None,
    date_rendering: str | None = None,
    has_text_layer: bool = True,
) -> Callable[[ExtractInvoiceTextInput], ExtractInvoiceTextOutput]:
    """The second seat. By default it reads the same thing the first one did."""
    primary = extraction or _extraction()
    agreed = second_read if second_read is not None else primary

    def _read(_payload: ExtractInvoiceTextInput) -> ExtractInvoiceTextOutput:
        if not has_text_layer:
            return ExtractInvoiceTextOutput(raw_text="", pages=[], has_text_layer=False)
        page = _page_text(primary, date_rendering)
        return ExtractInvoiceTextOutput(
            raw_text=page,
            pages=[page],
            has_text_layer=True,
            second_read=agreed,
            model_id="claude-haiku-4-5-20251001",
            prompt_version="extract_text_v1",
            input_tokens=1200,
            output_tokens=400,
            latency_ms=1500,
        )

    return _read


def _context(
    tmp_path: Path,
    extraction: InvoiceExtraction | None = None,
    **text_options: object,
) -> RunContext:
    """A run context whose two reading seats agree, with the real check between them."""
    return RunContext(
        run_id="run-1",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        ingest=_fake_ingest,
        extract=_fake_extract_factory(extraction),
        extract_text=_fake_text_factory(extraction, **text_options),  # type: ignore[arg-type]
    )


@pytest.fixture
def record(tmp_path: Path) -> InvoiceRecord:
    return InvoiceRecord(source_path=tmp_path / "invoice.pdf", created_at=utc_now())


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return _context(tmp_path)


# --- decide is pure ---------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "kind", "by"),
    [
        (InvoiceState.RECEIVED, ActionKind.INGEST_DOCUMENT, "rule"),
        (InvoiceState.INGESTED, ActionKind.COMPUTE_EXTRACTION_CONFIDENCE, "rule"),
        (InvoiceState.EXTRACTED, ActionKind.VALIDATE, "rule"),
        (InvoiceState.VALIDATED, ActionKind.STUB, "rule"),
        (InvoiceState.PENDING_APPROVAL, ActionKind.STUB, "rule"),
    ],
)
def test_decide_routes_by_state(
    record: InvoiceRecord, state: InvoiceState, kind: ActionKind, by: str
) -> None:
    action = decide(record.model_copy(update={"state": state}))
    assert action.kind is kind
    assert action.by == by


def test_decide_calls_nothing(record: InvoiceRecord) -> None:
    """No context is passed, so it cannot reach a tool even by accident.

    That is what makes the whole routing policy testable without a network.
    """
    for state in InvoiceState:
        assert decide(record.model_copy(update={"state": state})) is not None


def test_every_tool_has_an_action_kind() -> None:
    """A tool with no ActionKind would be unreachable from the loop forever."""
    assert set(TOOL_MODULE_NAMES) <= {kind.value for kind in ActionKind}


# --- apply never changes state ----------------------------------------------


def test_apply_returns_an_event_never_a_state(record: InvoiceRecord, ctx: RunContext) -> None:
    """The single most important property of the loop.

    apply() gathers data and reports what happened; only transition() decides
    what state follows. If apply could set state, the table would stop being the
    only description of what is possible.
    """
    updated, result = apply(decide(record), record, ctx)
    assert updated.state is InvoiceState.RECEIVED
    assert result.event == "ingest"


def test_apply_does_not_mutate_the_record_it_was_given(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    updated, _ = apply(decide(record), record, ctx)
    assert record.ingest is None
    assert updated.ingest is not None


def test_the_extraction_step_records_each_call_separately(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    """Three calls, three sets of numbers. Folding them together loses both.

    "What did the second reading cost" is a fair question of a system that pays
    two models to read the same page, and a single row averaging them cannot
    answer it.
    """
    ingested = record.model_copy(update={"state": InvoiceState.INGESTED})
    _, result = apply(decide(ingested), ingested, ctx)

    vision, text, scored = result.calls
    assert vision.kind is ActionKind.EXTRACT_INVOICE_VISION
    assert (vision.model_id, vision.input_tokens, vision.latency_ms) == (
        "claude-sonnet-5",
        7000,
        8000,
    )
    assert text.kind is ActionKind.EXTRACT_INVOICE_TEXT
    assert (text.model_id, text.input_tokens) == ("claude-haiku-4-5-20251001", 1200)
    assert scored.kind is ActionKind.COMPUTE_EXTRACTION_CONFIDENCE
    assert scored.model_id is None, "the check is code; attributing tokens to it would be a lie"


def test_the_extraction_step_stores_the_verdict_on_the_record(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    ingested = record.model_copy(update={"state": InvoiceState.INGESTED})
    updated, result = apply(decide(ingested), ingested, ctx)
    assert updated.confidence is not None
    assert updated.confidence.auto_ok is True
    assert result.event == "extract"


def test_the_decision_basis_names_every_load_bearing_field_and_the_date(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    """The row has to answer "why did this not go to a person" on its own."""
    ingested = record.model_copy(update={"state": InvoiceState.INGESTED})
    _, result = apply(decide(ingested), ingested, ctx)
    basis = result.decision_basis or ""
    for field in ("vendor_name", "invoice_number", "invoice_date", "currency", "total"):
        assert field in basis
    assert "date_verdict=unambiguous" in basis
    assert "subtotal" not in basis, "supporting fields would crowd out the ones that block"


# --- the full run -----------------------------------------------------------


def test_a_clean_invoice_reaches_closed(record: InvoiceRecord, ctx: RunContext) -> None:
    final = run(record, ctx)
    assert final.state is InvoiceState.CLOSED


def test_the_event_sequence_is_exactly_the_happy_path(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    run(record, ctx)
    assert [event.decision for event in ctx.events] == HAPPY_PATH_EVENTS


def test_the_trail_is_one_row_per_thing_that_happened(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    """More rows than steps, and step_seq counts rows.

    The audit contract says step_seq defines order. A step that makes three
    calls is four facts, and numbering them all by the step they belong to would
    leave four rows claiming the same position.
    """
    run(record, ctx)
    assert len(ctx.events) == len(HAPPY_PATH_EVENTS)
    assert [event.step_seq for event in ctx.events] == list(range(len(HAPPY_PATH_EVENTS)))


def test_only_the_last_row_of_a_step_moves_the_invoice(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    """So a reader following to_state down the file sees the state machine only."""
    run(record, ctx)
    moves = [event for event in ctx.events if event.to_state is not None]
    assert len(moves) == HAPPY_PATH_STEPS
    assert [event.decision for event in moves] == [
        "ingest",
        "extract",
        "validate",
        *["stub_ok"] * 11,
    ]


def test_the_trail_records_both_endpoints_of_every_move(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    run(record, ctx)
    moves = [event for event in ctx.events if event.to_state is not None]
    for event in moves:
        assert event.from_state is not None
    # Each move's destination is the next one's origin: no gaps.
    for earlier, later in pairwise(moves):
        assert earlier.to_state == later.from_state


def test_a_row_that_is_not_a_move_still_says_where_it_happened(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    """A call row has a from_state and no to_state. It reports, it does not move."""
    run(record, ctx)
    calls = [event for event in ctx.events if event.to_state is None]
    assert calls, "the extraction step makes three calls"
    assert all(event.from_state is not None for event in calls)


def test_the_run_leaves_a_verifiable_chain(record: InvoiceRecord, ctx: RunContext) -> None:
    run(record, ctx)
    assert isinstance(ctx.writer, JsonlAuditWriter)
    assert ctx.writer.verify(str(record.invoice_id))


# --- budgets ----------------------------------------------------------------


def test_max_steps_stops_the_run_and_logs_an_escalation(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    """An agent that can loop forever will eventually loop forever."""
    final = run(record, ctx, max_steps=4)
    assert final.state is not InvoiceState.CLOSED

    last = ctx.events[-1]
    assert last.decision == "escalated"
    assert last.event_type is AuditEventType.ERROR
    assert last.error_message is not None
    assert "4 steps" in last.error_message


def test_the_default_budget_fits_the_happy_path_with_no_room_to_spare() -> None:
    """If a step is added to the pipeline this must be raised deliberately.

    The budget counts loop iterations, not audit rows - a step that makes three
    calls is still one step, and spending the budget on rows would let a chatty
    step starve the pipeline.
    """
    assert HAPPY_PATH_STEPS + 1 == DEFAULT_MAX_STEPS


# --- failure routing --------------------------------------------------------


def test_a_failing_validation_routes_to_needs_human_extraction(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """Totals that do not add up are a document a person has to look at."""
    broken = _extraction(total=Decimal("999.00"))
    ctx = _context(tmp_path, broken)
    final = run(record, ctx)

    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    assert "totals_do_not_sum" in final.validation_flags
    moves = [event.decision for event in ctx.events if event.to_state is not None]
    assert moves == ["ingest", "extract", "validation_failed"]


def test_validation_reuses_the_contract_checks_rather_than_repeating_them() -> None:
    """One implementation of each arithmetic rule, not two that agree today.

    Every flag the step reports is either one the contract computed or one of the
    short list this step is allowed to add. If someone re-implements an
    arithmetic check here, its flag name will belong to neither set.
    """
    broken = _extraction(total=Decimal("999.00"), subtotal=Decimal("100.00"))
    record = InvoiceRecord(source_path=Path("x.pdf"), created_at=utc_now(), extraction=broken)
    flags, event = validate_extraction(record, utc_now())

    contract_flags = {flag.value for flag in ArithmeticFlag}
    assert set(flags) <= contract_flags | LOOP_ONLY_FLAGS
    # and it really did carry the contract's findings through
    assert {flag.value for flag in broken.arithmetic_flags} <= set(flags)
    assert event == "validation_failed"


def test_the_arithmetic_tolerances_live_in_exactly_one_place() -> None:
    """Proof the two are the same code: the contract's verdict is the step's.

    An amount inside the contract's tolerance must pass validation, and one
    outside must fail, without this module knowing what the tolerance is.
    """
    inside = _extraction(total=Decimal("120.01"))  # 1 cent, within TOTALS_TOLERANCE
    outside = _extraction(total=Decimal("120.05"))  # 5 cents, outside it

    assert inside.arithmetic_flags == []
    assert outside.arithmetic_flags != []

    for extraction, expected in ((inside, "validate"), (outside, "validation_failed")):
        record = InvoiceRecord(
            source_path=Path("x.pdf"), created_at=utc_now(), extraction=extraction
        )
        assert validate_extraction(record, utc_now())[1] == expected


def test_a_zero_total_is_a_policy_failure_not_an_arithmetic_one() -> None:
    """The contract sees nothing wrong with zero; the loop refuses to advance it."""
    zero = _extraction(subtotal=Decimal("0.00"), tax_total=Decimal("0.00"), total=Decimal("0.00"))
    record = InvoiceRecord(source_path=Path("x.pdf"), created_at=utc_now(), extraction=zero)
    flags, event = validate_extraction(record, utc_now())
    assert "total_is_zero" in flags
    assert event == "validation_failed"


def test_validation_without_an_extraction_fails_rather_than_crashing() -> None:
    record = InvoiceRecord(source_path=Path("x.pdf"), created_at=utc_now())
    flags, event = validate_extraction(record, utc_now())
    assert flags == ["no_extraction"]
    assert event == "validation_failed"


def test_a_future_invoice_date_fails_validation(record: InvoiceRecord, tmp_path: Path) -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=2)).date()
    ctx = _context(tmp_path, _extraction(invoice_date=tomorrow))
    final = run(record, ctx)
    assert "invoice_date_in_the_future" in final.validation_flags


def test_a_human_gated_state_halts_the_run_rather_than_erroring(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """The table refusing a stub is how the machine stops for a person.

    NEEDS_HUMAN_EXTRACTION has no stub edge, so the loop asks for a step, the
    table says no, and the run ends with that recorded.
    """
    ctx = _context(tmp_path, _extraction(total=Decimal("999.00")))
    final = run(record, ctx)
    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    halt = ctx.events[-1]
    assert halt.event_type is AuditEventType.NOTE
    assert halt.error_message is not None
    assert "no transition" in halt.error_message


def test_a_tool_that_raises_stops_the_run_without_raising(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """A failed invoice is a routing outcome, not a crash."""

    def _boom(_payload: IngestDocumentInput) -> IngestDocumentOutput:
        msg = "disk went away"
        raise OSError(msg)

    ctx = RunContext(
        run_id="run-1",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        ingest=_boom,
        extract=_fake_extract_factory(),
        extract_text=_fake_text_factory(),
    )
    final = run(record, ctx)

    assert final.state is InvoiceState.RECEIVED
    assert len(ctx.events) == 1
    assert ctx.events[0].event_type is AuditEventType.ERROR
    assert "disk went away" in (ctx.events[0].error_message or "")


def test_a_terminal_record_does_nothing(record: InvoiceRecord, ctx: RunContext) -> None:
    closed = record.model_copy(update={"state": InvoiceState.CLOSED})
    assert run(closed, ctx).state is InvoiceState.CLOSED
    assert ctx.events == []


# --- actors -----------------------------------------------------------------


def test_the_actor_says_who_is_accountable() -> None:
    """The first question asked of an AP trail."""
    empty = StepResult(event="x")
    tool = actor_for(Action(kind=ActionKind.INGEST_DOCUMENT, by="rule", rule_id="R"), empty)
    rule = actor_for(Action(kind=ActionKind.VALIDATE, by="rule", rule_id="VALIDATE@v1"), empty)
    human = actor_for(Action(kind=ActionKind.STUB, by="human"), empty)
    model = actor_for(
        Action(kind=ActionKind.EXTRACT_INVOICE_VISION, by="model"),
        StepResult(event="extract", model_id="claude-sonnet-5", prompt_version="extract_v1"),
    )

    assert isinstance(tool, ToolActor)
    assert tool.name == "ingest_document"
    assert isinstance(rule, RuleActor)
    assert rule.rule_id == "VALIDATE@v1"
    assert isinstance(human, HumanActor)
    assert isinstance(model, ModelActor)
    assert model.model_id == "claude-sonnet-5"


def test_each_reading_is_attributed_to_the_model_that_made_it(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    run(record, ctx)
    reads = [event for event in ctx.events if event.decision == "read"]
    assert [event.model_id for event in reads] == [
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    ]
    assert all(isinstance(event.actor, ModelActor) for event in reads)


def test_the_decision_is_attributed_to_the_rule_not_to_either_model(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    """Neither model decides anything here. The rule that scored them does."""
    run(record, ctx)
    decision = next(event for event in ctx.events if event.decision == "extract")
    assert isinstance(decision.actor, RuleActor)
    assert decision.actor.rule_id == "EXTRACT-CONF@v1"


# --- the ambiguous date, end to end -----------------------------------------
#
# Invoice 51109305 is the reason any of this exists. Its date reads 09/03/2024
# and the vision model has returned both 2024-09-03 and 2024-03-09 for it on
# different days. The loop's job is to stop treating that as a fact about the
# model and start treating it as a fact about the document.

AMBIGUOUS_RENDERING = "09/03/2024"
MARCH = date(2024, 3, 9)
SEPTEMBER = date(2024, 9, 3)


def _51109305(invoice_date: date = SEPTEMBER) -> InvoiceExtraction:
    return _extraction(
        vendor_name="TechVision Distributors Pvt Ltd",
        invoice_number="51109305",
        invoice_date=invoice_date,
        currency="INR",
        subtotal=Decimal("2023625.00"),
        tax_total=Decimal("202362.50"),
        total=Decimal("2225987.50"),
        line_items=[
            {
                "description": "27in IPS Monitor",
                "quantity": Decimal(1),
                "unit": "EA",
                "unit_price": Decimal("2023625.00"),
                "extended_price": Decimal("2023625.00"),
            }
        ],
    )


def _ambiguous_context(tmp_path: Path) -> RunContext:
    """The two seats read the same digits and order them differently.

    Which is exactly what happened live: same file, same models, twenty minutes
    apart, two different ISO dates.
    """
    return _context(
        tmp_path,
        _51109305(SEPTEMBER),
        second_read=_51109305(MARCH),
        date_rendering=AMBIGUOUS_RENDERING,
    )


def _replace_clock(ctx: RunContext, now: datetime) -> RunContext:
    """The same context on a fixed clock. Validation is the one time-dependent rule."""
    return replace(ctx, now=lambda: now)


def _received(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def test_an_ambiguous_date_does_not_fail_extraction(record: InvoiceRecord, tmp_path: Path) -> None:
    """The whole point. Two readings of the same digits is not a bad extraction."""
    ctx = _ambiguous_context(tmp_path)
    final = run(record.model_copy(update={"received_at": _received(date(2024, 3, 12))}), ctx)

    scored = next(event for event in ctx.events if event.decision == "extract")
    assert scored.to_state is InvoiceState.EXTRACTED
    assert final.confidence is not None
    assert final.confidence.auto_ok is True


def test_both_readings_are_carried_forward_from_extraction(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    ctx = _ambiguous_context(tmp_path)
    ingested = record.model_copy(update={"state": InvoiceState.INGESTED})
    updated, _ = apply(decide(ingested), ingested, ctx)

    assert updated.confidence is not None
    assert updated.confidence.date_verdict is DateVerdict.AMBIGUOUS
    assert updated.date_candidates == [MARCH, SEPTEMBER]
    assert updated.date_is_open is True
    assert updated.invoice_date_resolved is None


def test_the_receipt_window_settles_the_date_at_validation(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """Received 12 March, so 3 September had not happened yet. One reading left."""
    ctx = _ambiguous_context(tmp_path)
    final = run(record.model_copy(update={"received_at": _received(date(2024, 3, 12))}), ctx)

    assert final.state is InvoiceState.CLOSED
    assert final.invoice_date_resolved == MARCH
    assert final.date_resolution_reason is DateResolutionReason.RECEIPT_WINDOW
    assert final.date_is_open is False
    assert final.validation_flags == []


def test_the_receipt_window_resolution_is_its_own_audit_row(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """Reinterpreting a date is a decision, and it says what it saw and chose."""
    ctx = _ambiguous_context(tmp_path)
    run(record.model_copy(update={"received_at": _received(date(2024, 3, 12))}), ctx)

    resolution = next(
        event for event in ctx.events if event.decision == DateResolutionReason.RECEIPT_WINDOW.value
    )
    assert isinstance(resolution.actor, RuleActor)
    assert resolution.actor.rule_id == "DATE-RESOLVE@v1"
    assert resolution.to_state is None
    basis = resolution.decision_basis or ""
    assert "2024-03-09|2024-09-03" in basis
    assert "received_at=2024-03-12" in basis
    assert "chose=2024-03-09" in basis


def test_a_date_the_window_cannot_settle_stays_open_through_validation(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """Received in October, both readings are plausible. Not a validation failure."""
    ctx = _ambiguous_context(tmp_path)
    started = record.model_copy(update={"received_at": _received(date(2024, 10, 1))})
    part_way = run(started, ctx, max_steps=3)

    assert part_way.state is InvoiceState.VALIDATED
    assert part_way.date_is_open is True
    assert part_way.validation_flags == []


def test_the_vendors_country_settles_what_the_window_could_not(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """The stub hook. lookup_vendor will own this the moment it exists.

    Two phases because nothing puts a country on the record yet - in the
    finished pipeline lookup_vendor does it on the way into VENDOR_RESOLVED.
    """
    ctx = _ambiguous_context(tmp_path)
    started = record.model_copy(update={"received_at": _received(date(2024, 10, 1))})
    part_way = run(started, ctx, max_steps=3)
    assert part_way.date_is_open is True

    resumed = run(part_way.model_copy(update={"vendor_country": "IN"}), ctx)

    assert resumed.state is InvoiceState.CLOSED
    assert resumed.invoice_date_resolved == MARCH
    assert resumed.date_resolution_reason is DateResolutionReason.VENDOR_LOCALE


def test_the_locale_resolution_is_its_own_audit_row(record: InvoiceRecord, tmp_path: Path) -> None:
    ctx = _ambiguous_context(tmp_path)
    started = record.model_copy(update={"received_at": _received(date(2024, 10, 1))})
    part_way = run(started, ctx, max_steps=3)
    run(part_way.model_copy(update={"vendor_country": "IN"}), ctx)

    resolution = next(
        event for event in ctx.events if event.decision == DateResolutionReason.VENDOR_LOCALE.value
    )
    basis = resolution.decision_basis or ""
    assert "vendor_country=IN" in basis
    assert "chose=2024-03-09" in basis


def test_an_open_date_with_no_country_anywhere_stays_open(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """It reaches the end unresolved rather than being guessed at.

    Not a good outcome, and not one this session fixes: it is what the missing
    lookup_vendor costs. Recorded so the gap is visible rather than inferred.
    """
    ctx = _ambiguous_context(tmp_path)
    started = record.model_copy(update={"received_at": _received(date(2024, 10, 1))})
    final = run(started, ctx)

    assert final.state is InvoiceState.CLOSED
    assert final.date_is_open is True
    assert final.invoice_date_resolved is None


def test_an_unambiguous_invoice_needs_no_resolution_at_all(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """51109301's date is 03/07/2023, which both seats read the same way."""
    extraction = _extraction(
        vendor_name="TechVision Distributors Pvt Ltd",
        invoice_number="51109301",
        invoice_date=date(2023, 7, 3),
        currency="INR",
    )
    ctx = _context(tmp_path, extraction)
    final = run(record.model_copy(update={"received_at": _received(date(2023, 7, 10))}), ctx)

    assert final.state is InvoiceState.CLOSED
    assert final.confidence is not None
    assert final.confidence.auto_ok is True
    assert final.confidence.date_verdict is DateVerdict.UNAMBIGUOUS
    assert final.date_resolution_reason is None
    assert final.invoice_date_resolved == date(2023, 7, 3)


def test_one_impossible_reading_is_not_grounds_to_reject(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """Nobody has chosen between the readings yet, so one bad one proves nothing.

    Run on a clock where 9 March has happened and 3 September has not. The
    invoice must pass: the reading in the future may simply be the wrong
    reading, and rejecting on it would stop a valid invoice over a question the
    pipeline has not answered.
    """
    ctx = _replace_clock(_ambiguous_context(tmp_path), datetime(2024, 6, 1, tzinfo=UTC))
    final = run(record, ctx, max_steps=3)

    assert final.state is InvoiceState.VALIDATED
    assert final.validation_flags == []


def test_an_open_date_fails_only_when_every_reading_is_impossible(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """On a clock before both readings there is no reading that works."""
    ctx = _replace_clock(_ambiguous_context(tmp_path), datetime(2024, 1, 1, tzinfo=UTC))
    final = run(record, ctx, max_steps=3)

    assert "invoice_date_in_the_future" in final.validation_flags
    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION


# --- disagreement routes to a person ----------------------------------------


def test_two_readings_that_disagree_on_the_total_need_a_person(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """The failure that costs money, and the one grounding alone would miss."""
    ctx = _context(tmp_path, _extraction(), second_read=_extraction(total=Decimal("1200.00")))
    final = run(record, ctx)

    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    assert final.confidence is not None
    assert final.confidence.auto_ok is False
    assert "total" in final.confidence.blocking_fields()


def test_the_failure_row_names_the_field_a_person_must_open(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    ctx = _context(tmp_path, _extraction(), second_read=_extraction(total=Decimal("1200.00")))
    run(record, ctx)

    failure = next(event for event in ctx.events if event.decision == "extraction_failed")
    assert failure.to_state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    assert "needs_human=total:readings_disagree" in (failure.decision_basis or "")


def test_a_document_with_no_text_layer_is_never_automatic(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """A scan gets one reading, and one reading is not agreement."""
    ctx = _context(tmp_path, has_text_layer=False)
    final = run(record, ctx)

    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    assert final.confidence is not None
    assert final.confidence.auto_ok is False
    read = [event for event in ctx.events if event.decision == NO_SECOND_READ]
    assert len(read) == 1


# --- the chain holds throughout ---------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["happy", "ambiguous", "disagreed", "no_text_layer"],
)
def test_the_chain_verifies_after_every_kind_of_run(
    record: InvoiceRecord, tmp_path: Path, scenario: str
) -> None:
    """Including the runs that stop early.

    A trail with a hole exactly where things went wrong is worse than no trail,
    because it looks complete.
    """
    contexts = {
        "happy": lambda: _context(tmp_path),
        "ambiguous": lambda: _ambiguous_context(tmp_path),
        "disagreed": lambda: _context(
            tmp_path, _extraction(), second_read=_extraction(total=Decimal("1200.00"))
        ),
        "no_text_layer": lambda: _context(tmp_path, has_text_layer=False),
    }
    ctx = contexts[scenario]()
    run(record.model_copy(update={"received_at": _received(date(2026, 1, 3))}), ctx)

    assert isinstance(ctx.writer, JsonlAuditWriter)
    assert ctx.writer.verify(str(record.invoice_id))
    assert [event.step_seq for event in ctx.events] == list(range(len(ctx.events)))


# --- the stub hook is visible -----------------------------------------------


def test_the_stub_hook_is_declared() -> None:
    """A stub step is supposed to do nothing. This one does something.

    Listed so that deleting it when lookup_vendor becomes real is a deliberate
    act with a failing test, exactly like the stub edges.
    """
    assert frozenset({VENDOR_LOCALE_DATE_HOOK}) == STUB_HOOKS


def test_a_stub_step_does_nothing_anywhere_else(record: InvoiceRecord, ctx: RunContext) -> None:
    """The hook fires in one state and only when a date is actually open."""
    for state in (InvoiceState.VALIDATED, InvoiceState.MATCHED, InvoiceState.CODED):
        parked = record.model_copy(update={"state": state, "vendor_country": "IN"})
        updated, result = apply(decide(parked), parked, ctx)
        assert result.event == "stub_ok"
        assert result.calls == []
        assert updated.invoice_date_resolved is None
