"""durable runs: one row per position in a run, one unfinished run per invoice

Revision ID: 0006_durable_runs
Revises: 0005_documents_known_on_arrival
Create Date: 2026-10-12

A run is now a DBOS workflow, and DBOS runs each step *at least* once: a step
the process died inside is run again on recovery. Two constraints make the
repeat harmless and the recovery unambiguous.

``audit_events (run_id, step_seq)`` is unique. The loop's commit step writes its
row at a position its decide step fixed, so a second try collides here instead
of recording the same transition twice. It replaces ``(invoice_id, run_id,
step_seq)``, which it implies: a run belongs to one invoice. The trail is still
JSONL until the Postgres writer lands; the JSONL writer refuses the same
duplicate, and this is where the rule will bind when the rows move.

``runs (invoice_id) WHERE ended_at IS NULL`` is unique. A run that never
finished is one DBOS can recover, and `just run` attaches to it; a second
unfinished run for the same invoice would be a second process driving it.

Any run left unfinished before this revision predates DBOS and can never be
recovered, so it is closed as ``abandoned`` first - otherwise the index could
not be built, and the invoice could never be run again.

DBOS's own tables are not here. They live in the ``dbos`` schema, created by
``dbos migrate`` as the owner (``just db-migrate``), and Alembic does not look
at that schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_durable_runs"
down_revision: str | None = "0005_documents_known_on_arrival"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Tighten the audit position; allow one unfinished run per invoice."""
    op.drop_constraint(
        op.f("uq_audit_events_invoice_id_run_id_step_seq"), "audit_events", type_="unique"
    )
    op.create_unique_constraint(
        op.f("uq_audit_events_run_id_step_seq"), "audit_events", ["run_id", "step_seq"]
    )
    op.execute(
        "UPDATE runs SET ended_at = now(), last_step = 'abandoned' WHERE ended_at IS NULL"
    )
    op.create_index(
        "uq_runs_invoice_id_unfinished",
        "runs",
        ["invoice_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    """Restore 0005's constraints. Closed runs stay closed."""
    op.drop_index("uq_runs_invoice_id_unfinished", table_name="runs")
    op.drop_constraint(op.f("uq_audit_events_run_id_step_seq"), "audit_events", type_="unique")
    op.create_unique_constraint(
        op.f("uq_audit_events_invoice_id_run_id_step_seq"),
        "audit_events",
        ["invoice_id", "run_id", "step_seq"],
    )
