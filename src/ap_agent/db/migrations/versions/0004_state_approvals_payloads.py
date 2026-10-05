"""Day 4 schema: state, extractions, runs, approvals, dedup, payloads - and minimum grants

Revision ID: 0004_state_approvals_payloads
Revises: 0003_invoice_date_is_a_date
Create Date: 2026-10-05

What this adds, and the one thing it corrects.

* ``payloads`` - bytes addressed by their sha256, so every ``output_ref`` on the
  trail resolves to what was hashed. The application role may INSERT and SELECT
  and nothing else: the key is the content, so there is nothing to update.
* ``extractions`` - which readings (by payload ref) belong to an invoice, the
  confidence verdict, and a person's corrections.
* ``runs`` - one row per loop invocation: started, ended, steps, last step.
* ``approvals`` + ``approval_decisions`` - replaces ``approval_requests``, which
  was never written to (empty at this revision) and could not express a tier
  that needs two people. One approvals row per request with
  ``required_approvers``; one decision row per person, unique on
  ``(approval_id, decided_by)`` so two approvers are two people. Decisions are
  insert-only.
* ``dedup_index`` - what ``find_duplicates`` compares. Insert-only.
* ``invoices`` gains ``po_number``, ``erp_bill_id``, ``record_ref`` (the latest
  full record, as a payload), and a unique ``document_sha256``: one invoice per
  document, so running a file again resumes it.

**The correction.** Five indexes declared in the models were never created by
``0001`` - ``ix_invoices_state``, ``ix_invoices_vendor_id``, both
``audit_events`` indexes and ``ix_erp_writes_invoice_id``. Autogenerate found
the drift against a live database on 2026-10-05; they are created here.
Applied migrations are history and are corrected forward.

**Grants: the minimum, and no DELETE anywhere.** Rule 3 of ``CLAUDE.md`` says no
tool deletes anything, so the application role loses DELETE on the tables
``0002`` granted it on, and is given none on the new ones. ``audit_events`` is
unchanged: SELECT and INSERT. As in ``0002``, the revision grants to an existing
role and skips if it does not exist; provisioning creates roles.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_state_approvals_payloads"
down_revision: str | None = "0003_invoice_date_is_a_date"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = os.environ.get("AP_AGENT_DB_APP_ROLE", "ap_agent_app")

INSERT_ONLY_TABLES = ("payloads", "dedup_index", "approval_decisions")
"""Insert and read. Content-addressed, an index, and a record of who decided."""

READ_UPDATE_TABLES = ("extractions", "runs", "approvals")
"""Rows that are completed after they are created: a run ends, a request is decided."""

NO_DELETE_TABLES = ("documents", "invoices", "erp_writes")
"""Granted SELECT, INSERT, UPDATE, DELETE by 0002. DELETE is withdrawn."""


def _role_exists(connection: sa.Connection, role: str) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        ).scalar()
    )


def _grant() -> None:
    """The minimum the application needs on the new tables, and no DELETE anywhere."""
    if not _role_exists(op.get_bind(), APP_ROLE):
        print(  # noqa: T201 - alembic output is the only channel here
            f"role {APP_ROLE!r} does not exist; skipping grants. "
            "Create it during provisioning, then re-run this revision."
        )
        return
    for table in INSERT_ONLY_TABLES:
        op.execute(sa.text(f'GRANT SELECT, INSERT ON {table} TO "{APP_ROLE}"'))
    for table in READ_UPDATE_TABLES:
        op.execute(sa.text(f'GRANT SELECT, INSERT, UPDATE ON {table} TO "{APP_ROLE}"'))
    for table in NO_DELETE_TABLES:
        op.execute(sa.text(f'REVOKE DELETE ON {table} FROM "{APP_ROLE}"'))


def _ungrant() -> None:
    """Undo :func:`_grant`, restoring 0002's DELETE on the tables it covered."""
    if not _role_exists(op.get_bind(), APP_ROLE):
        return
    for table in (*INSERT_ONLY_TABLES, *READ_UPDATE_TABLES):
        op.execute(sa.text(f'REVOKE ALL ON {table} FROM "{APP_ROLE}"'))
    for table in NO_DELETE_TABLES:
        op.execute(sa.text(f'GRANT DELETE ON {table} TO "{APP_ROLE}"'))


def upgrade() -> None:
    """Create the Day 4 tables, the missing indexes, then grant the minimum."""
    op.create_table(
        "payloads",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("bytes", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_payloads_sha256_is_hex")),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_payloads")),
    )
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(length=26), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("required_approvers", sa.Integer(), nullable=False),
        sa.Column("approver_role", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("rule_basis", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'reject')",
            name=op.f("ck_approvals_decision_known"),
        ),
        sa.CheckConstraint(
            "status = 'pending' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name=op.f("ck_approvals_decided_requires_human"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name=op.f("ck_approvals_status_known"),
        ),
        sa.CheckConstraint(
            "required_approvers BETWEEN 1 AND 4", name=op.f("ck_approvals_required_approvers_range")
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.invoice_id"], name=op.f("fk_approvals_invoice_id_invoices")
        ),
        sa.PrimaryKeyConstraint("approval_id", name=op.f("pk_approvals")),
    )
    op.create_index("ix_approvals_invoice_id", "approvals", ["invoice_id"], unique=False)
    op.create_index("ix_approvals_status", "approvals", ["status"], unique=False)
    op.create_table(
        "dedup_index",
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("vendor_id", sa.String(length=64), nullable=False),
        sa.Column("normalised_invoice_number", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.invoice_id"], name=op.f("fk_dedup_index_invoice_id_invoices")
        ),
        sa.PrimaryKeyConstraint("invoice_id", name=op.f("pk_dedup_index")),
    )
    op.create_index("ix_dedup_index_invoice_date", "dedup_index", ["invoice_date"], unique=False)
    op.create_index(
        "ix_dedup_index_vendor_id_invoice_date",
        "dedup_index",
        ["vendor_id", "invoice_date"],
        unique=False,
    )
    op.create_table(
        "extractions",
        sa.Column("extraction_id", sa.String(length=26), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column("vision_ref", sa.String(length=64), nullable=False),
        sa.Column("text_ref", sa.String(length=64), nullable=True),
        sa.Column("confidence_ref", sa.String(length=64), nullable=True),
        sa.Column("auto_ok", sa.Boolean(), nullable=True),
        sa.Column("human_corrections", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("corrected_by", sa.String(length=64), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(human_corrections IS NULL) = (corrected_by IS NULL)",
            name=op.f("ck_extractions_correction_has_an_author"),
        ),
        sa.ForeignKeyConstraint(
            ["confidence_ref"],
            ["payloads.sha256"],
            name=op.f("fk_extractions_confidence_ref_payloads"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.invoice_id"], name=op.f("fk_extractions_invoice_id_invoices")
        ),
        sa.ForeignKeyConstraint(
            ["text_ref"], ["payloads.sha256"], name=op.f("fk_extractions_text_ref_payloads")
        ),
        sa.ForeignKeyConstraint(
            ["vision_ref"], ["payloads.sha256"], name=op.f("fk_extractions_vision_ref_payloads")
        ),
        sa.PrimaryKeyConstraint("extraction_id", name=op.f("pk_extractions")),
    )
    op.create_index("ix_extractions_invoice_id", "extractions", ["invoice_id"], unique=False)
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("invoice_id", sa.String(length=26), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("last_step", sa.String(length=64), nullable=True),
        sa.Column("last_state", sa.String(length=32), nullable=True),
        sa.CheckConstraint("step_count >= 0", name=op.f("ck_runs_step_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.invoice_id"], name=op.f("fk_runs_invoice_id_invoices")
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_runs")),
    )
    op.create_index("ix_runs_invoice_id", "runs", ["invoice_id"], unique=False)
    op.create_table(
        "approval_decisions",
        sa.Column("decision_id", sa.String(length=26), nullable=False),
        sa.Column("approval_id", sa.String(length=26), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')", name=op.f("ck_approval_decisions_decision_known")
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name=op.f("ck_approval_decisions_reason_required")
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.approval_id"],
            name=op.f("fk_approval_decisions_approval_id_approvals"),
        ),
        sa.PrimaryKeyConstraint("decision_id", name=op.f("pk_approval_decisions")),
        sa.UniqueConstraint(
            "approval_id", "decided_by", name=op.f("uq_approval_decisions_approval_id_decided_by")
        ),
    )
    op.drop_table("approval_requests")
    op.create_index(
        "ix_audit_events_invoice_id_step_seq",
        "audit_events",
        ["invoice_id", "step_seq"],
        unique=False,
    )
    op.create_index("ix_audit_events_ts_utc", "audit_events", ["ts_utc"], unique=False)
    op.create_index("ix_erp_writes_invoice_id", "erp_writes", ["invoice_id"], unique=False)
    op.add_column("invoices", sa.Column("po_number", sa.String(length=64), nullable=True))
    op.add_column("invoices", sa.Column("erp_bill_id", sa.String(length=64), nullable=True))
    op.add_column("invoices", sa.Column("record_ref", sa.String(length=64), nullable=True))
    op.create_index("ix_invoices_state", "invoices", ["state"], unique=False)
    op.create_index("ix_invoices_vendor_id", "invoices", ["vendor_id"], unique=False)
    op.create_unique_constraint(
        op.f("uq_invoices_document_sha256"), "invoices", ["document_sha256"]
    )
    op.create_foreign_key(
        op.f("fk_invoices_record_ref_payloads"), "invoices", "payloads", ["record_ref"], ["sha256"]
    )
    _grant()


def downgrade() -> None:
    """Withdraw the grants, then drop what upgrade created and restore approval_requests."""
    _ungrant()
    op.drop_constraint(op.f("fk_invoices_record_ref_payloads"), "invoices", type_="foreignkey")
    op.drop_constraint(op.f("uq_invoices_document_sha256"), "invoices", type_="unique")
    op.drop_index("ix_invoices_vendor_id", table_name="invoices")
    op.drop_index("ix_invoices_state", table_name="invoices")
    op.drop_column("invoices", "record_ref")
    op.drop_column("invoices", "erp_bill_id")
    op.drop_column("invoices", "po_number")
    op.drop_index("ix_erp_writes_invoice_id", table_name="erp_writes")
    op.drop_index("ix_audit_events_ts_utc", table_name="audit_events")
    op.drop_index("ix_audit_events_invoice_id_step_seq", table_name="audit_events")
    op.create_table(
        "approval_requests",
        sa.Column("approval_id", sa.VARCHAR(length=26), autoincrement=False, nullable=False),
        sa.Column("invoice_id", sa.VARCHAR(length=26), autoincrement=False, nullable=False),
        sa.Column("idempotency_key", sa.VARCHAR(length=128), autoincrement=False, nullable=False),
        sa.Column("approver_user_id", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column("reason", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("amount", sa.NUMERIC(precision=18, scale=2), autoincrement=False, nullable=False),
        sa.Column("currency", sa.VARCHAR(length=3), autoincrement=False, nullable=False),
        sa.Column("config_version", sa.VARCHAR(length=32), autoincrement=False, nullable=False),
        sa.Column("status", sa.VARCHAR(length=16), autoincrement=False, nullable=False),
        sa.Column("decided_by_user_id", sa.VARCHAR(length=64), autoincrement=False, nullable=True),
        sa.Column(
            "decided_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True
        ),
        sa.Column("decision_note", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column(
            "requested_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "due_by", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True
        ),
        sa.CheckConstraint(
            "status::text = 'pending'::text OR decided_by_user_id IS NOT NULL",
            name=op.f("ck_approval_requests_decided_requires_human"),
        ),
        sa.CheckConstraint(
            "status::text = ANY (ARRAY['pending'::character varying, 'approved'::character varying, 'rejected'::character varying, 'expired'::character varying, 'cancelled'::character varying]::text[])",
            name=op.f("ck_approval_requests_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.invoice_id"],
            name=op.f("fk_approval_requests_invoice_id_invoices"),
        ),
        sa.PrimaryKeyConstraint("approval_id", name=op.f("pk_approval_requests")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_approval_requests_idempotency_key"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.drop_table("approval_decisions")
    op.drop_index("ix_runs_invoice_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_extractions_invoice_id", table_name="extractions")
    op.drop_table("extractions")
    op.drop_index("ix_dedup_index_vendor_id_invoice_date", table_name="dedup_index")
    op.drop_index("ix_dedup_index_invoice_date", table_name="dedup_index")
    op.drop_table("dedup_index")
    op.drop_index("ix_approvals_status", table_name="approvals")
    op.drop_index("ix_approvals_invoice_id", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("payloads")
    if _role_exists(op.get_bind(), APP_ROLE):
        # As 0002 granted it, so a downgrade leaves the schema 0003 expects.
        op.execute(
            sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON approval_requests TO "{APP_ROLE}"')
        )
