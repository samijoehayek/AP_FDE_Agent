"""The decide/commit split: a repeated half writes nothing twice.

DBOS runs a step at least once - the step a process died inside is run again on
recovery. These tests repeat a half in process, with no DBOS and no database, and
assert the invariant the durable run depends on: one row per position, and a
transition row written once however many times its commit is tried.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
from tests.loop.test_runner import happy_context, make_extraction

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.contracts.audit import AuditEvent, SystemActor, utc_now
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.run import InvoiceRecord, StepOutcome
from ap_agent.errors import DuplicateAuditRowError
from ap_agent.loop.runner import (
    InProcess,
    Iterations,
    Progress,
    commit_iteration,
    decide_iteration,
    drive,
    escalation,
    run,
)
from ap_agent.persistence import InMemoryInvoiceStore
from ap_agent.states.machine import InvoiceState

if TYPE_CHECKING:
    from pathlib import Path

    from ap_agent.loop.runner import RunContext


def _note(run_id: str, step_seq: int, invoice_id: str = "inv-1") -> AuditEvent:
    return AuditEvent(
        invoice_id=invoice_id,
        run_id=run_id,
        step_seq=step_seq,
        ts_utc=utc_now(),
        actor=SystemActor(),
        event_type=AuditEventType.NOTE,
    )


# --- the writer: one row per position --------------------------------------


def test_a_second_row_at_a_taken_position_is_refused(tmp_path: Path) -> None:
    writer = JsonlAuditWriter(tmp_path)
    writer.append(_note("run-1", 0))
    with pytest.raises(DuplicateAuditRowError, match="run run-1, step 0"):
        writer.append(_note("run-1", 0))
    assert len(writer.read("inv-1")) == 1, "nothing was appended"


def test_positions_are_per_run(tmp_path: Path) -> None:
    """Two runs on one invoice each count from zero; the chain spans both."""
    writer = JsonlAuditWriter(tmp_path)
    writer.append(_note("run-1", 0))
    writer.append(_note("run-2", 0))
    writer.append(_note("run-1", 1))
    assert (writer.next_seq("inv-1", "run-1"), writer.next_seq("inv-1", "run-2")) == (2, 1)
    assert writer.next_seq("inv-1", "run-3") == 0
    assert writer.verify("inv-1")


def test_the_next_position_comes_from_the_file_not_the_process(tmp_path: Path) -> None:
    """A recovered run is a new process; a fresh writer must not start again at zero."""
    JsonlAuditWriter(tmp_path).append(_note("run-1", 0))
    assert JsonlAuditWriter(tmp_path).next_seq("inv-1", "run-1") == 1


# --- commit: harmless to repeat --------------------------------------------


@dataclass
class _DiesOnSave:
    """An invoice store whose next save never happens - the process died first."""

    inner: InMemoryInvoiceStore
    armed: bool = True
    saves: list[InvoiceState] = field(default_factory=list[InvoiceState])

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def save(self, record: InvoiceRecord) -> None:
        if self.armed:
            self.armed = False
            msg = "killed between the audit write and the state save"
            raise RuntimeError(msg)
        self.saves.append(record.state)
        self.inner.save(record)


def _stored(tmp_path: Path) -> tuple[RunContext, InMemoryInvoiceStore, InvoiceRecord]:
    store = InMemoryInvoiceStore()
    record = InvoiceRecord(source_path=tmp_path / "invoice.pdf", created_at=utc_now())
    store.create(record, document_sha256="b" * 64, byte_size=1, storage_uri="x")
    ctx = replace(happy_context(tmp_path / "trail", make_extraction()), store=store)
    return ctx, store, record


def _transition_rows(ctx: RunContext, invoice_id: str) -> list[tuple[str | None, str | None]]:
    rows = JsonlAuditWriter(tmp_path_of(ctx)).read(invoice_id)
    return [
        (row.from_state, row.to_state)
        for row in rows
        if row.event_type is AuditEventType.STATE_TRANSITION
    ]


def tmp_path_of(ctx: RunContext) -> Path:
    writer = ctx.writer
    assert isinstance(writer, JsonlAuditWriter)
    return writer.path_for("x").parent


def test_a_commit_killed_after_its_row_writes_it_once_when_tried_again(tmp_path: Path) -> None:
    """The Piece 2 gap: row written, state not saved, process gone. Then recovery."""
    ctx, store, record = _stored(tmp_path)
    dying = _DiesOnSave(store)
    ctx = replace(ctx, store=dying)  # type: ignore[arg-type] - a stand-in that forwards the rest
    invoice_id = str(record.invoice_id)

    outcome = decide_iteration(record, ctx)  # checkpointed by DBOS: not repeated
    with pytest.raises(RuntimeError, match="killed"):
        commit_iteration(ctx, outcome)
    assert store.load(invoice_id).state is InvoiceState.RECEIVED, "the save never happened"

    commit_iteration(ctx, outcome)  # recovery runs the commit step again

    assert _transition_rows(ctx, invoice_id) == [(InvoiceState.RECEIVED, InvoiceState.INGESTED)]
    assert store.load(invoice_id).state is InvoiceState.INGESTED
    assert JsonlAuditWriter(tmp_path_of(ctx)).verify(invoice_id)


def test_a_commit_that_completed_changes_nothing_when_tried_again(tmp_path: Path) -> None:
    ctx, store, record = _stored(tmp_path)
    outcome = decide_iteration(record, ctx)
    commit_iteration(ctx, outcome)
    rows_before = JsonlAuditWriter(tmp_path_of(ctx)).read(str(record.invoice_id))

    commit_iteration(ctx, outcome)

    assert JsonlAuditWriter(tmp_path_of(ctx)).read(str(record.invoice_id)) == rows_before
    assert store.load(str(record.invoice_id)).state is InvoiceState.INGESTED


def test_a_repeated_decide_reserves_a_new_position_and_records_the_repeat_calls(
    tmp_path: Path,
) -> None:
    """A decide that died is run again in full: its calls really happened twice.

    Two decide outcomes for the same step are possible only if the first never
    reached its checkpoint - so only the second is ever committed, and the
    transition is still written once.
    """
    ctx, _store, record = _stored(tmp_path)
    ctx = replace(ctx, store=None)
    moved = record.model_copy(update={"state": InvoiceState.INGESTED})

    lost = decide_iteration(moved, ctx)  # the run died before this was checkpointed
    kept = decide_iteration(moved, ctx)
    commit_iteration(ctx, kept)

    assert lost.step_seq is not None
    assert kept.step_seq is not None
    assert kept.step_seq > lost.step_seq
    rows = JsonlAuditWriter(tmp_path_of(ctx)).read(str(record.invoice_id))
    reads = Counter(row.event_type for row in rows)
    assert reads[AuditEventType.MODEL_CALL] == 4, "two readings, twice: both were paid for"
    assert reads[AuditEventType.STATE_TRANSITION] == 1


def test_escalation_is_written_once_however_often_its_commit_runs(tmp_path: Path) -> None:
    ctx, _store, record = _stored(tmp_path)
    ctx = replace(ctx, store=None)
    outcome = escalation(record, ctx, max_steps=3)
    commit_iteration(ctx, outcome)
    commit_iteration(ctx, outcome)
    rows = JsonlAuditWriter(tmp_path_of(ctx)).read(str(record.invoice_id))
    assert [row.decision for row in rows] == ["escalated"]


# --- what a checkpoint holds ------------------------------------------------


def test_a_checkpoint_carries_a_reference_to_the_invoice_never_the_invoice(
    tmp_path: Path,
) -> None:
    """DBOS stores the decide step's output in its own tables. No document text goes there."""
    ctx, store, record = _stored(tmp_path)
    remit = "Pay to IBAN XX00APAGENT0000000000001"
    ctx = replace(
        happy_context(tmp_path / "t2", make_extraction(remit_to_display=remit)), store=store
    )

    outcome = run_until_extracted(record, ctx)

    checkpoint = outcome.model_dump_json()
    assert "Acme Ltd" not in checkpoint
    assert "XX00APAGENT" not in checkpoint
    assert outcome.record_ref.startswith("sha256:")


def run_until_extracted(record: InvoiceRecord, ctx: RunContext) -> StepOutcome:
    commit_iteration(ctx, decide_iteration(record, ctx))  # RECEIVED -> INGESTED
    assert ctx.store is not None
    outcome = decide_iteration(ctx.store.load(str(record.invoice_id)), ctx)
    assert outcome.to_state is InvoiceState.EXTRACTED
    return outcome


# --- the driver is the same either way --------------------------------------


@dataclass
class _Recording:
    """Iterations that record what the driver asked for."""

    inner: Iterations
    calls: list[str] = field(default_factory=list[str])

    def decide(self) -> StepOutcome:
        self.calls.append("decide")
        return self.inner.decide()

    def commit(self, outcome: StepOutcome) -> None:
        self.calls.append(f"commit:{outcome.kind}")
        self.inner.commit(outcome)

    def escalate(self, max_steps: int) -> None:
        self.calls.append("escalate")
        self.inner.escalate(max_steps)


def test_the_driver_alternates_and_escalates_once(tmp_path: Path) -> None:
    ctx, _store, record = _stored(tmp_path)
    recording = _Recording(InProcess(record, replace(ctx, store=None)))
    progress = Progress()

    drive(recording, 2, progress)

    assert recording.calls == [
        "decide",
        "commit:transition",
        "decide",
        "commit:transition",
        "escalate",
    ]
    assert progress.steps == 2


def test_a_run_still_reaches_approval_through_the_split(tmp_path: Path) -> None:
    ctx, _store, record = _stored(tmp_path)
    final = run(record, ctx)
    assert final.state is InvoiceState.PENDING_APPROVAL
    transitions = _transition_rows(ctx, str(record.invoice_id))
    assert len(transitions) == len(set(transitions)) == 8
