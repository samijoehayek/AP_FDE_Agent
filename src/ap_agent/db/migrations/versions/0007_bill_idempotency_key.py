"""bill idempotency key: one bill per vendor and invoice number

Revision ID: 0007_bill_idempotency_key
Revises: 0006_durable_runs
Create Date: 2026-10-12

``invoices.erp_idempotency_key`` is the key ``create_bill`` writes before it
calls QuickBooks: ``bill:<vendor_id>:<normalised invoice number>``. Unique, so
two invoices can never claim the same bill, and written first, so a process
that dies after QuickBooks answered has left the key behind for the retry to
look up by.

The key is a pair, not the invoice id, on purpose: the same paper scanned twice
is two invoice ids and one bill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_bill_idempotency_key"
down_revision: str | None = "0006_durable_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the key column, unique."""
    op.add_column(
        "invoices", sa.Column("erp_idempotency_key", sa.String(length=128), nullable=True)
    )
    op.create_unique_constraint(
        op.f("uq_invoices_erp_idempotency_key"), "invoices", ["erp_idempotency_key"]
    )


def downgrade() -> None:
    """Drop it."""
    op.drop_constraint(op.f("uq_invoices_erp_idempotency_key"), "invoices", type_="unique")
    op.drop_column("invoices", "erp_idempotency_key")
