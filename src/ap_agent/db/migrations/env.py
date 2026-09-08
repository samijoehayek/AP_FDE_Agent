"""Alembic environment.

The URL comes from :func:`ap_agent.config.get_settings`, never from
``alembic.ini`` - a committed file must not be able to hold a password, and a
migration must run against the same database the application does.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ap_agent.config import get_settings
from ap_agent.db.base import Base
from ap_agent.db.models import (
    ApprovalRequest,
    AuditEventRow,
    Document,
    ErpWrite,
    Invoice,
)

REGISTERED_TABLES = (ApprovalRequest, AuditEventRow, Document, ErpWrite, Invoice)
"""Named so the imports are not pruned.

Importing these classes is what registers their tables on ``Base.metadata``,
and ``Base.metadata`` is what ``--autogenerate`` diffs the database against. A
model that is not imported here is a table Alembic will silently propose to
drop.
"""

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. Used to review a migration."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations in a transaction."""
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
