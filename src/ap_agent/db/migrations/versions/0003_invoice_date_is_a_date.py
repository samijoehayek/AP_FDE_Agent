"""store invoice_date as a calendar date, not a timestamp

Revision ID: 0003_invoice_date_is_a_date
Revises: 0002_grant_app_role
Create Date: 2026-09-09

``invoices.invoice_date`` was TIMESTAMP WITH TIME ZONE while the contract that
feeds it, ``InvoiceExtraction.invoice_date``, is a ``datetime.date``.

An invoice date is a calendar date printed on a document. It has no time and no
timezone, and storing it as timestamptz invents both: Postgres attaches the
session TimeZone on the way in and converts on the way out, so the same row can
read as a different day depending on who is asking. In this system that is not
cosmetic - invoice_date drives payment terms and the duplicate-detection window,
so a day's drift is a wrong due date or a duplicate that slips through.

Every other temporal column in the schema is a genuine instant (``*_at``,
``ts_utc``, ``due_by``) and correctly stays timestamptz.

Safe as written: the column is nullable and the table is empty at this revision.
Against existing rows the USING clause takes the date part in UTC, which is the
value that was meant all along.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_invoice_date_is_a_date"
down_revision: str | None = "0002_grant_app_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Narrow invoice_date to DATE."""
    op.alter_column(
        "invoices",
        "invoice_date",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.Date(),
        existing_nullable=True,
        postgresql_using="(invoice_date AT TIME ZONE 'UTC')::date",
    )


def downgrade() -> None:
    """Widen it back. Midnight UTC is the only defensible time to invent."""
    op.alter_column(
        "invoices",
        "invoice_date",
        existing_type=sa.Date(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="invoice_date::timestamptz",
    )
