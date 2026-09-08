"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-09

Generated from the SQLAlchemy models, then edited by hand for the one thing
autogenerate cannot know: ``audit_events`` is append-only, and that is enforced
by revoking UPDATE, DELETE, and TRUNCATE rather than by an ORM convention. A
convention is one bug away from being wrong; a missing grant is not.

``AUDIT_WRITER_ROLE`` defaults to the role running the migration, which is
correct for local development where the application and the migration share a
connection string. In production, run migrations as a dedicated migration role
and set ``AP_AGENT_DB_APP_ROLE`` to the application's role so the revoke lands on
the account that actually serves traffic - and so the migration role retains the
rights it needs to alter the table later.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("audit_events",)
AUDIT_WRITER_ROLE = os.environ.get("AP_AGENT_DB_APP_ROLE", "CURRENT_USER")


def upgrade() -> None:
    """Create every table, then lock the audit trail down."""
    op.create_table('audit_events',
    sa.Column('event_id', sa.String(length=26), nullable=False),
    sa.Column('invoice_id', sa.String(length=26), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('step_seq', sa.Integer(), nullable=False),
    sa.Column('ts_utc', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_kind', sa.String(length=16), nullable=False),
    sa.Column('actor', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('from_state', sa.String(length=32), nullable=True),
    sa.Column('to_state', sa.String(length=32), nullable=True),
    sa.Column('input_ref', sa.String(length=512), nullable=True),
    sa.Column('output_ref', sa.String(length=512), nullable=True),
    sa.Column('tool_name', sa.String(length=128), nullable=True),
    sa.Column('tool_args_redacted', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('tool_result_summary', sa.Text(), nullable=True),
    sa.Column('model_id', sa.String(length=128), nullable=True),
    sa.Column('prompt_version', sa.String(length=32), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('trace_id', sa.String(length=128), nullable=True),
    sa.Column('decision', sa.String(length=64), nullable=True),
    sa.Column('decision_basis', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('error_class', sa.String(length=128), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('prev_event_hash', sa.String(length=64), nullable=False),
    sa.Column('event_hash', sa.String(length=64), nullable=False),
    sa.CheckConstraint('step_seq >= 0', name=op.f('ck_audit_events_step_seq_non_negative')),
    sa.PrimaryKeyConstraint('event_id', name=op.f('pk_audit_events')),
    sa.UniqueConstraint('invoice_id', 'run_id', 'step_seq', name=op.f('uq_audit_events_invoice_id_run_id_step_seq'))
    )
    op.create_table('documents',
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('storage_uri', sa.Text(), nullable=False),
    sa.Column('media_type', sa.String(length=64), nullable=False),
    sa.Column('byte_size', sa.BigInteger(), nullable=False),
    sa.Column('page_count', sa.Integer(), nullable=False),
    sa.Column('has_text_layer', sa.Boolean(), nullable=False),
    sa.Column('pages_with_text', sa.Integer(), nullable=False),
    sa.Column('min_sharpness', sa.Float(), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('byte_size > 0', name=op.f('ck_documents_byte_size_positive')),
    sa.CheckConstraint('page_count >= 1', name=op.f('ck_documents_page_count_positive')),
    sa.PrimaryKeyConstraint('sha256', name=op.f('pk_documents'))
    )
    op.create_table('erp_writes',
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('invoice_id', sa.String(length=26), nullable=False),
    sa.Column('operation', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=64), nullable=True),
    sa.Column('request_digest', sa.String(length=64), nullable=True),
    sa.Column('response_summary', sa.Text(), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('claimed', 'succeeded', 'failed')", name=op.f('ck_erp_writes_status_known')),
    sa.PrimaryKeyConstraint('idempotency_key', name=op.f('pk_erp_writes'))
    )
    op.create_table('invoices',
    sa.Column('invoice_id', sa.String(length=26), nullable=False),
    sa.Column('document_sha256', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('vendor_id', sa.String(length=64), nullable=True),
    sa.Column('vendor_name', sa.String(length=200), nullable=True),
    sa.Column('invoice_number', sa.String(length=64), nullable=True),
    sa.Column('invoice_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('total', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('extraction', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('match_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('config_version', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('total IS NULL OR total >= 0', name=op.f('ck_invoices_total_non_negative')),
    sa.ForeignKeyConstraint(['document_sha256'], ['documents.sha256'], name=op.f('fk_invoices_document_sha256_documents')),
    sa.PrimaryKeyConstraint('invoice_id', name=op.f('pk_invoices')),
    sa.UniqueConstraint('vendor_id', 'invoice_number', name=op.f('uq_invoices_vendor_id_invoice_number'))
    )
    op.create_table('approval_requests',
    sa.Column('approval_id', sa.String(length=26), nullable=False),
    sa.Column('invoice_id', sa.String(length=26), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('approver_user_id', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('config_version', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('decided_by_user_id', sa.String(length=64), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('due_by', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status = 'pending' OR decided_by_user_id IS NOT NULL", name=op.f('ck_approval_requests_decided_requires_human')),
    sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')", name=op.f('ck_approval_requests_status_known')),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.invoice_id'], name=op.f('fk_approval_requests_invoice_id_invoices')),
    sa.PrimaryKeyConstraint('approval_id', name=op.f('pk_approval_requests')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_approval_requests_idempotency_key'))
    )

    for table in APPEND_ONLY_TABLES:
        op.execute(
            sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {table} FROM {AUDIT_WRITER_ROLE}")
        )


def downgrade() -> None:
    """Drop everything. This destroys the audit trail: development only."""
    op.drop_table('approval_requests')
    op.drop_table('invoices')
    op.drop_table('erp_writes')
    op.drop_table('documents')
    op.drop_table('audit_events')
