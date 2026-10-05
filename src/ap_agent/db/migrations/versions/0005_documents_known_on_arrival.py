"""documents are recorded on arrival; parse facts are filled in by intake

Revision ID: 0005_documents_known_on_arrival
Revises: 0004_state_approvals_payloads
Create Date: 2026-10-05

Resume is keyed by the document's sha256: running the same file again finds its
invoice and carries on from where it stopped. For that, the ``documents`` and
``invoices`` rows have to exist from the moment the file arrives - before intake
has parsed anything - so a crash during intake restarts intake for the *same*
invoice instead of creating a second one.

So the columns only parsing can fill (media type, page count, text layer) are
nullable until ``ingest_document`` runs, and ``page_count`` may be zero: intake
refuses some files before opening them (wrong type, too large), and zero is how
``IngestDocumentOutput`` says so. ``byte_size > 0`` stays; an empty file is
refused before any row is written.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_documents_known_on_arrival"
down_revision: str | None = "0004_state_approvals_payloads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Relax the parse-derived columns and the page-count check."""
    op.drop_constraint(op.f("ck_documents_page_count_positive"), "documents", type_="check")
    op.alter_column("documents", "media_type", existing_type=sa.String(64), nullable=True)
    op.alter_column("documents", "page_count", existing_type=sa.Integer(), nullable=True)
    op.alter_column("documents", "has_text_layer", existing_type=sa.Boolean(), nullable=True)
    op.alter_column("documents", "pages_with_text", existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint(
        op.f("ck_documents_page_count_known"),
        "documents",
        "page_count IS NULL OR page_count >= 0",
    )


def downgrade() -> None:
    """Restore 0004's constraints. Fails if any document is still unparsed - by design."""
    op.drop_constraint(op.f("ck_documents_page_count_known"), "documents", type_="check")
    op.alter_column("documents", "pages_with_text", existing_type=sa.Integer(), nullable=False)
    op.alter_column("documents", "has_text_layer", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("documents", "page_count", existing_type=sa.Integer(), nullable=False)
    op.alter_column("documents", "media_type", existing_type=sa.String(64), nullable=False)
    op.create_check_constraint(
        op.f("ck_documents_page_count_positive"), "documents", "page_count >= 1"
    )
