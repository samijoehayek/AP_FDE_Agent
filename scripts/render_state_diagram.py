"""Regenerate the state diagram in ``docs/ARCHITECTURE.md`` from the code.

The diagram is written between two marker comments in the document, so it can be
regenerated without touching the prose around it. A hand-maintained diagram of a
47-edge state machine is wrong within a week; this one cannot drift from the
table it documents.

Usage:
    just docs-diagram
    uv run python scripts/render_state_diagram.py --check   # CI-friendly
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from ap_agent.states.machine import to_mermaid

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "docs" / "ARCHITECTURE.md"
BEGIN = "<!-- BEGIN GENERATED STATE DIAGRAM -->"
END = "<!-- END GENERATED STATE DIAGRAM -->"

app = typer.Typer(add_completion=False, help=__doc__)


def _render(document: str) -> str:
    start = document.index(BEGIN) + len(BEGIN)
    stop = document.index(END)
    block = f"\n\n```mermaid\n{to_mermaid()}\n```\n\n"
    return document[:start] + block + document[stop:]


@app.command()
def main(
    check: Annotated[
        bool, typer.Option("--check", help="Exit non-zero if the file is out of date.")
    ] = False,
) -> None:
    """Write the current transition table into the architecture document."""
    document = TARGET.read_text(encoding="utf-8")
    if BEGIN not in document or END not in document:
        typer.secho(f"markers not found in {TARGET}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    updated = _render(document)
    if updated == document:
        typer.echo("state diagram is up to date")
        return

    if check:
        typer.secho("state diagram is out of date: run `just docs-diagram`", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    TARGET.write_text(updated, encoding="utf-8")
    typer.secho(f"regenerated the state diagram in {TARGET.name}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
