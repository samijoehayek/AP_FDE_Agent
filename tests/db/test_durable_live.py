"""Durable runs against a live database: kill a run, recover it, count the rows.

Each crash test runs ``durable_child.py`` twice. The first process dies on
purpose (``os._exit``) at a chosen point; the second launches DBOS under the
same run's executor id, which recovers the run and finishes it. The only things
the two processes share are Postgres and the trail directory.

What is asserted is the invariant, not the mechanism: every transition the run
made is on the trail exactly once, the chain verifies, the stored state is
where the run ended, and the calls a checkpointed step made are not repeated.

Rows are written as the application role and removed afterwards as the schema
owner, DBOS's included. Skipped unless revision 0006 and the ``dbos`` schema
are both present.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.pool import NullPool
from tests.loop.test_runner import happy_context, make_extraction
from typer.testing import CliRunner
from ulid import ULID

from ap_agent import cli as cli_module
from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.cli import app
from ap_agent.config import REPO_ROOT, Settings
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.db.base import create_session_factory
from ap_agent.db.store import PostgresApprovalStore, PostgresDedupIndex, PostgresInvoiceStore
from ap_agent.loop import durable
from ap_agent.payloads import PostgresPayloadStore
from ap_agent.states.machine import InvoiceState
from ap_agent.tools.ingest_document import ingest_document

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from typer.testing import Result

CHILD = REPO_ROOT / "tests" / "db" / "durable_child.py"
KILLED = 9
HAPPY_PATH_TRANSITIONS = 8
"""RECEIVED -> PENDING_APPROVAL with every seat agreeing."""
LOCK_REFUSAL_BOUND_SECONDS = 15
"""Generous: a refusal takes milliseconds; a lock that waits never returns at all."""


def _engine(url_field: str) -> sa.Engine:
    return sa.create_engine(Settings.model_fields[url_field].default, poolclass=NullPool)


def _ready() -> bool:
    try:
        with _engine("database_url").connect() as connection:
            return bool(
                connection.execute(
                    sa.text(
                        "SELECT to_regclass('public.uq_runs_invoice_id_unfinished') IS NOT NULL "
                        "AND to_regclass('dbos.workflow_status') IS NOT NULL"
                    )
                ).scalar()
            )
    except (OperationalError, DBAPIError):
        return False


pytestmark = pytest.mark.skipif(not _ready(), reason="no database at 0006 with a dbos schema")


class Invoice:
    """One stored invoice, its document, a run id, and where its trail goes."""

    def __init__(self, store: PostgresInvoiceStore, tmp_path: Path) -> None:
        self.store = store
        self.pdf = tmp_path / "invoice.pdf"
        canvas = Canvas(str(self.pdf), pagesize=A4)
        canvas.drawString(72, 760, f"INVOICE {ULID()} total 120.00")
        canvas.showPage()
        canvas.save()
        record = InvoiceRecord(source_path=self.pdf, created_at=utc_now())
        content = self.pdf.read_bytes()
        self.sha = hashlib.sha256(content).hexdigest()
        store.create(record, document_sha256=self.sha, byte_size=len(content), storage_uri="x")
        self.invoice_id = str(record.invoice_id)
        self.run_id = f"run-{ULID()}"
        self.number = f"DUR-{ULID()}"
        self.audit_dir = tmp_path

    def child(self, mode: str) -> subprocess.CompletedProcess[str]:
        """Run the child in ``mode`` against this invoice and run."""
        argv = [
            "uv",
            "run",
            "python",
            str(CHILD),
            mode,
            self.invoice_id,
            self.run_id,
            str(self.audit_dir),
            self.number,
        ]
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, our own script
            argv, cwd=REPO_ROOT, capture_output=True, text=True, timeout=180, check=False
        )

    @property
    def trail(self) -> JsonlAuditWriter:
        return JsonlAuditWriter(self.audit_dir / "audit")

    def transitions(self) -> list[tuple[str | None, str | None]]:
        return [
            (row.from_state, row.to_state)
            for row in self.trail.read(self.invoice_id)
            if row.event_type is AuditEventType.STATE_TRANSITION
        ]

    def model_calls(self) -> int:
        return Counter(row.event_type for row in self.trail.read(self.invoice_id))[
            AuditEventType.MODEL_CALL
        ]


@pytest.fixture
def invoice(tmp_path: Path) -> Iterator[Invoice]:
    sessions = create_session_factory(_engine("database_url"))
    created = Invoice(PostgresInvoiceStore(sessions, PostgresPayloadStore(sessions)), tmp_path)
    yield created
    with _engine("database_migration_url").begin() as owner:
        params = {"i": created.invoice_id, "r": created.run_id, "s": created.sha}
        for statement in (
            (
                "DELETE FROM approval_decisions WHERE approval_id IN "
                "(SELECT approval_id FROM approvals WHERE invoice_id = :i)"
            ),
            "DELETE FROM approvals WHERE invoice_id = :i",
            "DELETE FROM extractions WHERE invoice_id = :i",
            "DELETE FROM runs WHERE invoice_id = :i",
            "DELETE FROM dedup_index WHERE invoice_id = :i",
            "DELETE FROM invoices WHERE invoice_id = :i",
            "DELETE FROM documents WHERE sha256 = :s",
            "DELETE FROM dbos.operation_outputs WHERE workflow_uuid = :r",
            "DELETE FROM dbos.workflow_input WHERE workflow_uuid = :r",
            "DELETE FROM dbos.workflow_output WHERE workflow_uuid = :r",
            "DELETE FROM dbos.workflow_status WHERE workflow_uuid = :r",
        ):
            owner.execute(sa.text(statement), params)


# --- the crash tests ---------------------------------------------------------


def test_a_run_killed_between_its_row_and_its_save_recovers_with_one_row(
    invoice: Invoice,
) -> None:
    """Kill between the audit write and the state save; recover; one row per transition."""
    killed = invoice.child("kill-commit")

    assert killed.returncode == KILLED, killed.stderr
    assert (InvoiceState.INGESTED, InvoiceState.EXTRACTED) in invoice.transitions()
    assert invoice.store.load(invoice.invoice_id).state is InvoiceState.INGESTED, "never saved"
    assert durable.workflow_status(invoice.run_id) == "PENDING"
    assert invoice.store.unfinished_run(invoice.invoice_id) == invoice.run_id

    recovered = invoice.child("finish")

    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip().endswith(InvoiceState.PENDING_APPROVAL.value)
    transitions = invoice.transitions()
    assert len(transitions) == len(set(transitions)) == HAPPY_PATH_TRANSITIONS
    assert invoice.trail.verify(invoice.invoice_id)
    assert invoice.model_calls() == 2, "the extraction step was checkpointed: not read again"
    assert invoice.store.load(invoice.invoice_id).state is InvoiceState.PENDING_APPROVAL
    assert invoice.store.unfinished_run(invoice.invoice_id) is None
    assert durable.workflow_status(invoice.run_id) == "SUCCESS"


def test_a_run_killed_inside_a_step_runs_that_step_again_and_still_moves_once(
    invoice: Invoice,
) -> None:
    """Kill mid-step, after one paid reading; recovery reads again, and says so."""
    killed = invoice.child("kill-decide")

    assert killed.returncode == KILLED, killed.stderr
    assert invoice.model_calls() == 1, "the vision reading returned before the process died"
    assert invoice.store.load(invoice.invoice_id).state is InvoiceState.INGESTED

    recovered = invoice.child("finish")

    assert recovered.returncode == 0, recovered.stderr
    transitions = invoice.transitions()
    assert len(transitions) == len(set(transitions)) == HAPPY_PATH_TRANSITIONS
    assert invoice.model_calls() == 3, "at least once: the step ran again, and both runs cost"
    assert invoice.trail.verify(invoice.invoice_id)
    assert invoice.store.load(invoice.invoice_id).state is InvoiceState.PENDING_APPROVAL


def test_a_second_process_is_turned_away_while_the_invoice_is_held(invoice: Invoice) -> None:
    """The lock is what makes "free" mean "nobody is running it" - and so safe to recover."""
    with invoice.store.lock(invoice.invoice_id) as held:
        assert held
        with invoice.store.lock(invoice.invoice_id) as again:
            assert not again, "a second holder in the same process is refused too"
        argv = ["uv", "run", "ap-agent", "run", str(invoice.pdf), "--audit-dir"]
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, our own CLI
            [*argv, str(invoice.audit_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    assert result.returncode == 1
    assert f"invoice {invoice.invoice_id} is being run by another process" in result.stderr
    assert not invoice.trail.read(invoice.invoice_id), "not one row written"


def test_recover_skips_a_run_whose_invoice_is_held_and_does_not_wait(
    invoice: Invoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``just recover`` beside a live run: refused at once, nothing driven, nothing written.

    DBOS is told the run is pending, so the lock is the only thing standing
    between ``recover`` and a second process running the same steps. The CLI
    runs on a thread: a lock that waited instead of refusing would hang this
    test, so it is bounded and reported as a failure instead.
    """
    invoice.store.start_run(invoice.run_id, invoice.invoice_id)
    driven: list[str] = []

    def _record(invoice_id: str, _ctx: object, _max_steps: int) -> None:
        driven.append(invoice_id)

    def _pending(_run_id: str) -> str:
        return "PENDING"

    monkeypatch.setattr(cli_module, "run_durably", _record)
    monkeypatch.setattr(cli_module, "workflow_status", _pending)
    outcome: list[Result] = []
    with invoice.store.lock(invoice.invoice_id) as held:
        assert held
        started = time.monotonic()
        worker = threading.Thread(
            target=lambda: outcome.append(
                CliRunner().invoke(app, ["recover", "--audit-dir", str(invoice.audit_dir)])
            ),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=LOCK_REFUSAL_BOUND_SECONDS)
        elapsed = time.monotonic() - started
        waited = worker.is_alive()
    worker.join()  # if it waited, let it finish now, while the substitutes are still in place
    assert not waited, "recover waited for the lock instead of refusing it"

    (result,) = outcome
    assert result.exit_code == 0, result.output
    assert f"skipped     : {invoice.invoice_id} (run {invoice.run_id})" in result.output
    assert invoice.invoice_id not in driven
    assert invoice.store.unfinished_run(invoice.invoice_id) == invoice.run_id, "not abandoned"
    assert invoice.store.load(invoice.invoice_id).state is InvoiceState.RECEIVED
    assert not invoice.trail.read(invoice.invoice_id), "not one row written"
    assert elapsed < LOCK_REFUSAL_BOUND_SECONDS


def test_a_durable_run_in_this_process_closes_everything_it_opened(invoice: Invoice) -> None:
    """Twice, in one process: DBOS is launched and destroyed per run, and leaks nothing.

    Warnings are errors in this suite, so an unclosed pooled connection fails it.
    """
    sessions = create_session_factory(_engine("database_url"))
    ctx = replace(
        happy_context(invoice.audit_dir, make_extraction(invoice_number=invoice.number)),
        run_id=invoice.run_id,
        ingest=ingest_document,
        payloads=invoice.store.payloads,
        store=invoice.store,
        dedup=PostgresDedupIndex(sessions),
        approvals=PostgresApprovalStore(sessions),
    )

    durable.execute(invoice.invoice_id, ctx, max_steps=3)
    assert durable.workflow_status(invoice.run_id) == "SUCCESS"
    durable.execute(invoice.invoice_id, ctx, max_steps=3)  # same id: the recorded result

    rows = invoice.trail.read(invoice.invoice_id)
    assert [row.decision for row in rows if row.event_type is AuditEventType.ERROR] == [
        "escalated"
    ], "the budget ran out once, and the second execute replayed nothing"
    assert len(invoice.transitions()) == 3
    assert invoice.store.unfinished_run(invoice.invoice_id) is None


# --- the constraints that back it --------------------------------------------


def _audit_row(run_id: str, step_seq: int, invoice_id: str) -> dict[str, object]:
    return {
        "e": str(ULID()),
        "i": invoice_id,
        "r": run_id,
        "q": step_seq,
        "h": "0" * 64,
    }


def test_two_audit_rows_cannot_share_a_run_and_position(invoice: Invoice) -> None:
    insert = sa.text(
        "INSERT INTO audit_events (event_id, invoice_id, run_id, step_seq, ts_utc, actor_kind, "
        "actor, event_type, retry_count, prev_event_hash, event_hash) VALUES "
        "(:e, :i, :r, :q, now(), 'system', '{}'::jsonb, 'note', 0, :h, :h)"
    )
    with _engine("database_url").connect() as app:
        transaction = app.begin()
        try:
            app.execute(insert, _audit_row(invoice.run_id, 0, invoice.invoice_id))
            app.execute(insert, _audit_row(invoice.run_id, 1, invoice.invoice_id))
            with pytest.raises(IntegrityError, match="uq_audit_events_run_id_step_seq"):
                app.execute(insert, _audit_row(invoice.run_id, 1, invoice.invoice_id))
        finally:
            transaction.rollback()


def test_an_invoice_has_at_most_one_unfinished_run(invoice: Invoice) -> None:
    insert = sa.text("INSERT INTO runs (run_id, invoice_id, step_count) VALUES (:r, :i, 0)")
    finish = sa.text("UPDATE runs SET ended_at = now() WHERE run_id = :r")
    with _engine("database_url").connect() as app:
        transaction = app.begin()
        try:
            first, second, third = (f"run-{ULID()}" for _ in range(3))
            app.execute(insert, {"r": first, "i": invoice.invoice_id})
            app.execute(finish, {"r": first})
            app.execute(insert, {"r": second, "i": invoice.invoice_id})  # one open: fine
            with pytest.raises(IntegrityError, match="uq_runs_invoice_id_unfinished"):
                app.execute(insert, {"r": third, "i": invoice.invoice_id})
        finally:
            transaction.rollback()
