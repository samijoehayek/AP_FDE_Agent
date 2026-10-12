"""One durable run in its own process, for the crash tests - and, on request, its death.

Run by ``test_durable_live.py`` as ``uv run python tests/db/durable_child.py
<mode> <invoice_id> <run_id> <audit_dir> <invoice_number>``; never collected by
pytest. Every seat is the happy path's fake, so nothing here reaches a model or
QuickBooks; the stores are Postgres as the application role and the run is a
real DBOS workflow.

Modes:

* ``kill-commit`` - dies inside the state save that would move the invoice to
  EXTRACTED: its transition row is already on the trail, the state is not
  saved. The gap Piece 2 left open.
* ``kill-decide`` - dies inside the text reading, after the vision reading's
  row is on the trail: a step that never reached its checkpoint.
* ``approve-kill-post`` - two people approve the waiting invoice, and the run
  that follows dies inside ``create_bill``, after QuickBooks holds the bill and
  before this process hears back. The bill exists; nothing here knows it.
* ``finish`` - no death. Starts the run, or recovers it if it is pending.

QuickBooks is ``tests/qbo_fake.py`` backed by ``<audit_dir>/qbo.json``, so the
process that dies and the one that recovers share one ledger.

``os._exit``, not an exception: no ``finally`` runs, no connection is closed
politely. As close to ``kill -9`` as a process can do to itself.
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy.pool import NullPool
from tests.loop.test_post import untaxed
from tests.loop.test_runner import happy_context
from tests.qbo_fake import FakeQbo

from ap_agent.contracts.run import InvoiceRecord
from ap_agent.db.base import create_db_engine, create_session_factory
from ap_agent.db.store import (
    PostgresApprovalStore,
    PostgresDedupIndex,
    PostgresInvoiceStore,
)
from ap_agent.loop import durable
from ap_agent.loop.runner import DEFAULT_MAX_STEPS, apply_human_decision
from ap_agent.payloads import PostgresPayloadStore
from ap_agent.states.machine import InvoiceEvent, InvoiceState
from ap_agent.tools.extract_invoice_text import (
    ExtractInvoiceTextInput,
    ExtractInvoiceTextOutput,
)
from ap_agent.tools.ingest_document import ingest_document

KILLED = 9


@dataclass(frozen=True)
class _DiesBeforeSavingExtracted(PostgresInvoiceStore):
    """Writes nothing for the save that would record EXTRACTED; the process is gone."""

    def save(self, record: InvoiceRecord) -> None:
        if record.state is InvoiceState.EXTRACTED:
            os._exit(KILLED)
        super().save(record)


def _dies_reading_text(_payload: ExtractInvoiceTextInput) -> ExtractInvoiceTextOutput:
    os._exit(KILLED)


def _die() -> None:
    os._exit(KILLED)


def main(mode: str, invoice_id: str, run_id: str, audit_dir: str, invoice_number: str) -> None:
    """Run ``run_id`` on ``invoice_id`` durably, dying where ``mode`` says."""
    qbo = FakeQbo(path=Path(audit_dir) / "qbo.json")
    if mode == "approve-kill-post":
        qbo.after_create = _die
    # By module path: the package re-exports the function under the module's name.
    setattr(importlib.import_module("ap_agent.tools.create_bill"), "_client", lambda: qbo)  # noqa: B010
    sessions = create_session_factory(create_db_engine(poolclass=NullPool))
    payloads = PostgresPayloadStore(sessions)
    store_type = _DiesBeforeSavingExtracted if mode == "kill-commit" else PostgresInvoiceStore
    store = store_type(sessions, payloads)
    ctx = replace(
        happy_context(Path(audit_dir), untaxed(invoice_number=invoice_number)),
        run_id=run_id,
        ingest=ingest_document,
        payloads=payloads,
        store=store,
        dedup=PostgresDedupIndex(sessions),
        approvals=PostgresApprovalStore(sessions),
    )
    if mode == "kill-decide":
        ctx = replace(ctx, extract_text=_dies_reading_text)
    with store.lock(invoice_id) as held:
        if not held:
            sys.exit("the invoice is locked by another process")
        if mode == "approve-kill-post":
            for person in ("alice", "bob"):
                record = store.load(invoice_id)
                if record.state is InvoiceState.PENDING_APPROVAL:
                    apply_human_decision(
                        record, ctx, event=InvoiceEvent.APPROVE, user_id=person, reason="ok"
                    )
        durable.execute(invoice_id, ctx, DEFAULT_MAX_STEPS)
    print(store.load(invoice_id).state.value)


if __name__ == "__main__":
    main(*sys.argv[1:6])
