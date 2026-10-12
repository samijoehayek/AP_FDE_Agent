"""Create or upgrade DBOS's tables in the ``dbos`` schema, as the owner.

DBOS can migrate its own tables when it launches, but that would need the role
it launches as - the application role - to hold DDL rights. It does not. So the
owner runs this, from ``just db-migrate`` after Alembic, and the application
launches with ``run_migrations`` off.

The grants DBOS gives the application role are all inside ``dbos``: usage on the
schema, all privileges on its tables and sequences, execute on its functions.
Nothing in ``public`` is touched. DELETE is among "all privileges" - DBOS
deletes its own bookkeeping - which is why the no-DELETE rule is asserted for
``public`` (``tests/db/test_live_grants.py``) and the ``dbos`` schema is its
one exception. Measured with ``dbos migrate --print-user-role`` and
``--print-migrations all`` before granting anything; see DECISIONS.md.
"""

from __future__ import annotations

import typer
from dbos import DBOS
from sqlalchemy.engine import make_url

from ap_agent.config import get_settings
from ap_agent.loop.durable import SYSTEM_SCHEMA

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main() -> None:
    """Migrate the ``dbos`` schema as the owner; grant the application role its use."""
    settings = get_settings()
    app_role = make_url(settings.database_url).username
    if app_role is None or app_role == make_url(settings.database_migration_url).username:
        typer.secho(
            "DATABASE_URL must name the application role, not the owner",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    DBOS.migrate(settings.database_migration_url, schema=SYSTEM_SCHEMA, application_role=app_role)
    typer.echo(f"dbos schema migrated; {app_role} granted its use")


if __name__ == "__main__":
    app()
