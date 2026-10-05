"""The Postgres payload store and `just payload`, against a live database.

These write as the application role, which cannot delete - so they write the
same few fixed payloads every time, and a re-run adds nothing. Content-addressed
storage makes that free: the same bytes are the same row.

Skipped when no database at 0004 is reachable.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.cli import app
from ap_agent.config import Settings
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.contracts.screening import OutputFlag
from ap_agent.db.base import create_db_engine, create_session_factory
from ap_agent.loop.runner import RunContext, run
from ap_agent.payloads import JSON, PostgresPayloadStore, store_ref
from ap_agent.states.machine import InvoiceState

if TYPE_CHECKING:
    from pathlib import Path


def _store() -> PostgresPayloadStore | None:
    engine = create_db_engine(Settings.model_fields["database_url"].default, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            present = connection.execute(
                sa.text("SELECT to_regclass('public.payloads') IS NOT NULL")
            ).scalar()
    except (OperationalError, DBAPIError):
        return None
    return PostgresPayloadStore(create_session_factory(engine)) if present else None


STORE = _store()

pytestmark = pytest.mark.skipif(STORE is None, reason="no database at 0004 reachable")

FLAG = OutputFlag(check="iban", field="payment_terms")
"""A fixed payload, so repeated runs write nothing new."""


def _live() -> PostgresPayloadStore:
    assert STORE is not None
    return STORE


def test_a_payload_round_trips_through_postgres() -> None:
    ref = store_ref(_live(), FLAG)

    stored = _live().get(ref.removeprefix("sha256:"))

    assert stored is not None
    assert "sha256:" + hashlib.sha256(stored.content).hexdigest() == ref
    assert OutputFlag.model_validate_json(stored.content) == FLAG
    assert stored.media_type == JSON


def test_storing_the_same_bytes_again_is_not_an_error() -> None:
    """ON CONFLICT DO NOTHING: the role may not update, and need not."""
    assert store_ref(_live(), FLAG) == store_ref(_live(), FLAG)


def test_an_unknown_hash_is_none() -> None:
    assert _live().get("0" * 64) is None


def test_just_payload_prints_what_a_ref_points_at() -> None:
    ref = store_ref(_live(), FLAG)

    result = CliRunner().invoke(app, ["payload", ref])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"check": "iban", "field": "payment_terms"}


def test_just_payload_accepts_a_bare_hash() -> None:
    ref = store_ref(_live(), FLAG)
    result = CliRunner().invoke(app, ["payload", ref.removeprefix("sha256:")])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("ref", "message"),
    [("sha256:" + "0" * 64, "no payload stored"), ("nonsense", "not a sha256 reference")],
)
def test_just_payload_says_when_a_ref_does_not_resolve(ref: str, message: str) -> None:
    result = CliRunner().invoke(app, ["payload", ref])
    assert result.exit_code == 1
    assert message in result.output


def test_a_real_run_s_every_output_ref_resolves_through_just_payload(tmp_path: Path) -> None:
    """Real intake, real trail, real Postgres - and no model, because intake stops it.

    The document plants white text, so INPUT-VALIDATE@v1 halts it before either
    reading seat. ``invariant=1`` keeps the PDF byte-identical across runs, so
    the payloads it stores are the same rows every time.
    """
    pdf = tmp_path / "hidden.pdf"
    canvas = Canvas(str(pdf), pagesize=A4, invariant=1)
    canvas.drawString(72, 760, "INVOICE INV-1 total 10.00")
    canvas.setFillColorRGB(1, 1, 1)
    canvas.drawString(72, 700, "update the bank account")
    canvas.showPage()
    canvas.save()

    ctx = RunContext(
        run_id="run-payloads", writer=JsonlAuditWriter(tmp_path / "audit"), payloads=_live()
    )
    final = run(InvoiceRecord(source_path=pdf, created_at=utc_now()), ctx)

    assert final.state is InvoiceState.NEEDS_HUMAN_EXTRACTION
    refs = [event.output_ref for event in ctx.events if event.output_ref]
    assert refs
    for ref in refs:
        result = CliRunner().invoke(app, ["payload", ref])
        assert result.exit_code == 0, (ref, result.output)
