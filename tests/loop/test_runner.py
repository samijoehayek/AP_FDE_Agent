"""The agent loop, driven entirely offline.

The extraction seat is injected through ``RunContext``, so no test here can
reach the API even if the guard in conftest were removed. That is deliberate:
the loop's job is routing, and routing should be testable without a network,
a database or a key.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.contracts.audit import HumanActor, ModelActor, RuleActor, ToolActor, utc_now
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.run import Action, ActionKind, InvoiceRecord, StepResult
from ap_agent.loop.runner import (
    DEFAULT_MAX_STEPS,
    RunContext,
    actor_for,
    apply,
    decide,
    run,
)
from ap_agent.states.machine import InvoiceState
from ap_agent.tools import TOOL_MODULE_NAMES
from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionOutput
from ap_agent.tools.ingest_document import IngestDocumentInput, IngestDocumentOutput

if TYPE_CHECKING:
    from collections.abc import Callable

    from ap_agent.tools.extract_invoice_vision import ExtractInvoiceVisionInput

HAPPY_PATH_EVENTS = [
    "ingest",
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


@pytest.fixture
def record(tmp_path: Path) -> InvoiceRecord:
    return InvoiceRecord(source_path=tmp_path / "invoice.pdf", created_at=utc_now())


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        run_id="run-1",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        ingest=_fake_ingest,
        extract=_fake_extract_factory(),
    )


# --- decide is pure ---------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "kind", "by"),
    [
        (InvoiceState.RECEIVED, ActionKind.INGEST_DOCUMENT, "rule"),
        (InvoiceState.INGESTED, ActionKind.EXTRACT_INVOICE_VISION, "model"),
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


def test_extraction_records_the_model_accounting(record: InvoiceRecord, ctx: RunContext) -> None:
    ingested = record.model_copy(update={"state": InvoiceState.INGESTED})
    _, result = apply(decide(ingested), ingested, ctx)
    assert result.model_id == "claude-sonnet-5"
    assert result.prompt_version == "extract_v1"
    assert result.input_tokens == 7000
    assert result.latency_ms == 8000


# --- the full run -----------------------------------------------------------


def test_a_clean_invoice_reaches_closed(record: InvoiceRecord, ctx: RunContext) -> None:
    final = run(record, ctx)
    assert final.state is InvoiceState.CLOSED


def test_the_event_sequence_is_exactly_the_happy_path(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    run(record, ctx)
    assert [event.decision for event in ctx.events] == HAPPY_PATH_EVENTS


def test_every_step_writes_exactly_one_audit_event(record: InvoiceRecord, ctx: RunContext) -> None:
    run(record, ctx)
    assert len(ctx.events) == len(HAPPY_PATH_EVENTS)
    assert [event.step_seq for event in ctx.events] == list(range(len(HAPPY_PATH_EVENTS)))


def test_the_trail_records_both_endpoints_of_every_move(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    run(record, ctx)
    for event in ctx.events:
        assert event.from_state is not None
        assert event.to_state is not None
    # Each event's destination is the next one's origin: no gaps.
    for earlier, later in zip(ctx.events, ctx.events[1:], strict=False):
        assert earlier.to_state == later.from_state


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
    """If a step is added to the pipeline this must be raised deliberately."""
    assert len(HAPPY_PATH_EVENTS) + 1 == DEFAULT_MAX_STEPS


# --- failure routing --------------------------------------------------------


def test_a_failing_validation_routes_to_needs_human_extraction(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """Totals that do not add up are a document a person has to look at."""
    broken = _extraction(total=Decimal("999.00"))
    ctx = RunContext(
        run_id="run-1",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        ingest=_fake_ingest,
        extract=_fake_extract_factory(broken),
    )
    final = run(record, ctx)

    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    assert "totals_do_not_sum" in final.validation_flags
    assert [event.decision for event in ctx.events][:3] == [
        "ingest",
        "extract",
        "validation_failed",
    ]


def test_a_future_invoice_date_fails_validation(record: InvoiceRecord, tmp_path: Path) -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=2)).date()
    ctx = RunContext(
        run_id="run-1",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        ingest=_fake_ingest,
        extract=_fake_extract_factory(_extraction(invoice_date=tomorrow)),
    )
    final = run(record, ctx)
    assert "invoice_date_in_the_future" in final.validation_flags


def test_a_human_gated_state_halts_the_run_rather_than_erroring(
    record: InvoiceRecord, tmp_path: Path
) -> None:
    """The table refusing a stub is how the machine stops for a person.

    NEEDS_HUMAN_EXTRACTION has no stub edge, so the loop asks for a step, the
    table says no, and the run ends with that recorded.
    """
    ctx = RunContext(
        run_id="run-1",
        writer=JsonlAuditWriter(tmp_path / "audit"),
        ingest=_fake_ingest,
        extract=_fake_extract_factory(_extraction(total=Decimal("999.00"))),
    )
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


def test_the_extraction_step_is_attributed_to_the_model(
    record: InvoiceRecord, ctx: RunContext
) -> None:
    run(record, ctx)
    extract_event = next(event for event in ctx.events if event.decision == "extract")
    assert isinstance(extract_event.actor, ModelActor)
    assert extract_event.actor.prompt_version == "extract_v1"
