"""Command-line entry points.

Only commands backed by implemented code are wired up. ``ap-agent run`` exists
and fails loudly rather than being absent, so that the shape of the finished
system is visible from ``--help`` on day one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ap_agent import __version__
from ap_agent.errors import APAgentError
from ap_agent.states.machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    InvoiceState,
    allowed_events,
    to_mermaid,
    transition,
)
from ap_agent.tools.ingest_document import IngestDocumentInput, ingest_document

app = typer.Typer(
    name="ap-agent",
    help="Accounts-payable invoice processing.",
    no_args_is_help=True,
    add_completion=False,
)
states_app = typer.Typer(help="Inspect the invoice state machine.", no_args_is_help=True)
app.add_typer(states_app, name="states")


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(help="Document to ingest.")],
    no_sharpness: Annotated[
        bool, typer.Option("--no-sharpness", help="Skip page rendering.")
    ] = False,
) -> None:
    """Hash, identify, and quality-check one document."""
    try:
        result = ingest_document(IngestDocumentInput(path=path, compute_sharpness=not no_sharpness))
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    payload = result.model_dump(mode="json")
    payload["is_born_digital"] = result.is_born_digital
    payload["min_sharpness"] = result.min_sharpness
    typer.echo(json.dumps(payload, indent=2))


@states_app.command("graph")
def states_graph() -> None:
    """Print the transition table as a Mermaid state diagram."""
    typer.echo(to_mermaid())


@states_app.command("list")
def states_list() -> None:
    """List every state with its legal events."""
    for state in InvoiceState:
        marker = " (terminal)" if state in TERMINAL_STATES else ""
        events = ", ".join(sorted(allowed_events(state))) or "-"
        typer.echo(f"{state.value}{marker}: {events}")
    typer.echo(f"\n{len(TRANSITIONS)} transitions over {len(InvoiceState)} states.")


@states_app.command("check")
def states_check(
    from_state: Annotated[InvoiceState, typer.Argument(help="Current state.")],
    event: Annotated[str, typer.Argument(help="Event to apply.")],
) -> None:
    """Show the state an event leads to, or fail if the transition is illegal."""
    try:
        typer.echo(transition(from_state, event).value)
    except APAgentError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@app.command(name="run")
def run(
    invoice_id: Annotated[str, typer.Argument(help="Invoice to advance.")],
) -> None:
    """Advance one invoice through the pipeline. NOT IMPLEMENTED."""
    typer.secho(
        f"the agent loop is not implemented yet (invoice {invoice_id})",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


if __name__ == "__main__":  # pragma: no cover
    app()
