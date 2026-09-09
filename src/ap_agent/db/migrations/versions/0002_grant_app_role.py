"""grant the application role, and make audit_events append-only for real

Revision ID: 0002_grant_app_role
Revises: 0001_initial
Create Date: 2026-09-09

0001 revoked UPDATE, DELETE and TRUNCATE on audit_events FROM CURRENT_USER, and
that revoke did nothing. The Postgres Docker image makes POSTGRES_USER a
SUPERUSER, and a superuser bypasses every privilege check - so the revoke landed
in the table ACL and was then ignored on every query. Verified against a live
container: the grant list showed only INSERT/SELECT/REFERENCES/TRIGGER, and an
UPDATE still succeeded.

The control only works against a role that is subject to grants, so this
revision moves it onto one. ``ap_agent`` continues to own the schema and run
migrations; ``AP_AGENT_DB_APP_ROLE`` (default ``ap_agent_app``) is what the
application connects as, and is an ordinary role.

The revision does not create that role. Provisioning creates roles - the compose
init script locally, your infrastructure in production - and migrations grant
privileges. Keeping that split is what stops a password ending up in version
control. If the role does not exist the revision logs and skips, so a throwaway
database (CI's service container) still migrates cleanly.

0001 is left exactly as it is. It has been applied; an applied migration is
history, and history gets corrected forward.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_grant_app_role"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = os.environ.get("AP_AGENT_DB_APP_ROLE", "ap_agent_app")

APPEND_ONLY_TABLES = ("audit_events",)
"""Insert and read. Never update, delete, or truncate."""

READ_WRITE_TABLES = ("documents", "invoices", "approval_requests", "erp_writes")
"""Ordinary application tables."""


def _role_exists(connection: sa.Connection, role: str) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        ).scalar()
    )


def upgrade() -> None:
    """Grant the application role only what it needs."""
    connection = op.get_bind()
    if not _role_exists(connection, APP_ROLE):
        print(  # noqa: T201 - alembic output is the only channel here
            f"role {APP_ROLE!r} does not exist; skipping grants. "
            "Create it during provisioning, then re-run this revision."
        )
        return

    op.execute(sa.text(f'GRANT USAGE ON SCHEMA public TO "{APP_ROLE}"'))

    for table in READ_WRITE_TABLES:
        op.execute(sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{APP_ROLE}"'))

    # The whole point. Insert and read; nothing that can rewrite what happened.
    for table in APPEND_ONLY_TABLES:
        op.execute(sa.text(f'GRANT SELECT, INSERT ON {table} TO "{APP_ROLE}"'))
        op.execute(sa.text(f'REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM "{APP_ROLE}"'))

    # Alembic's own bookkeeping table: the application never touches it.
    op.execute(sa.text(f'REVOKE ALL ON alembic_version FROM "{APP_ROLE}"'))


def downgrade() -> None:
    """Withdraw every grant made above."""
    connection = op.get_bind()
    if not _role_exists(connection, APP_ROLE):
        return
    for table in (*READ_WRITE_TABLES, *APPEND_ONLY_TABLES):
        op.execute(sa.text(f'REVOKE ALL ON {table} FROM "{APP_ROLE}"'))
    op.execute(sa.text(f'REVOKE USAGE ON SCHEMA public FROM "{APP_ROLE}"'))
