"""Prove that ``audit_events`` really is append-only. Exits non-zero if not.

This script exists because the control silently did not work once. Revision 0001
revoked UPDATE and DELETE, the revoke was recorded in the table's ACL, and it
had no effect at all: the application connected as the schema owner, the
Postgres image makes that role a SUPERUSER, and a superuser bypasses every
privilege check. The documentation said "append-only at the schema level" and
the database disagreed.

A control nobody exercises is a control nobody can trust, so this runs in CI on
every push and can be run by hand after any change to the audit schema:

    just verify-audit

It connects as the *application* role - not the owner - because that is the role
that has to be constrained. Reading the ACL is not enough; the ACL looked
correct while the control was broken. So this issues the statements and checks
that the database refuses them.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import psycopg
import typer

from ap_agent.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

app = typer.Typer(add_completion=False, help=__doc__)

FORBIDDEN = (
    ("UPDATE", "UPDATE audit_events SET decision = 'tampered'"),
    ("DELETE", "DELETE FROM audit_events"),
    ("TRUNCATE", "TRUNCATE audit_events"),
)
"""Everything that could rewrite history. Each must be refused."""

ALLOWED_READ = "SELECT count(*) FROM audit_events"


def _dsn(url: str) -> str:
    """Strip SQLAlchemy's driver suffix; psycopg wants a plain libpq URL."""
    return url.replace("postgresql+psycopg://", "postgresql://")


def _checks(connection: psycopg.Connection[tuple[object, ...]]) -> Iterator[tuple[str, bool, str]]:
    """Yield ``(name, passed, detail)`` for each statement that must be refused."""
    for name, statement in FORBIDDEN:
        try:
            connection.execute(statement)  # type: ignore[arg-type]
        except psycopg.errors.InsufficientPrivilege as exc:
            yield name, True, str(exc).strip().splitlines()[0]
        except psycopg.Error as exc:
            yield name, False, f"refused, but for the wrong reason: {type(exc).__name__}: {exc}"
        else:
            yield name, False, "SUCCEEDED - the audit trail can be rewritten"
        finally:
            connection.rollback()


@app.command()
def main() -> None:
    """Check that the application role cannot rewrite the audit trail."""
    settings = get_settings()
    dsn = _dsn(settings.database_url)

    if dsn == _dsn(settings.database_migration_url):
        typer.secho(
            "DATABASE_URL and DATABASE_MIGRATION_URL name the same role. The application "
            "must not connect as the schema owner - the owner is a superuser and bypasses "
            "the grants this script is checking.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    with psycopg.connect(dsn) as connection:
        (is_super,) = connection.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone() or (None,)
        if is_super:
            typer.secho(
                f"the application connects as a SUPERUSER ({dsn.split('@')[-1]}); "
                "every grant on audit_events is bypassed and the control is decorative.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

        connection.execute(ALLOWED_READ)
        typer.secho("SELECT  allowed", fg=typer.colors.GREEN)

        failures = 0
        for name, passed, detail in _checks(connection):
            if passed:
                typer.secho(f"{name:<7} refused  ({detail})", fg=typer.colors.GREEN)
            else:
                failures += 1
                typer.secho(f"{name:<7} NOT REFUSED  {detail}", fg=typer.colors.RED, err=True)

    if failures:
        typer.secho(
            f"\n{failures} statement(s) the audit trail must refuse were allowed.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    typer.secho("\naudit_events is append-only for the application role.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
