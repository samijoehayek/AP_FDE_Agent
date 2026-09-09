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
from ulid import ULID

from ap_agent import __version__
from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.config import REPO_ROOT
from ap_agent.contracts.audit import utc_now
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.errors import APAgentError
from ap_agent.loop.runner import DEFAULT_MAX_STEPS, RunContext
from ap_agent.loop.runner import run as loop_run
from ap_agent.states.machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    InvoiceState,
    allowed_events,
    to_mermaid,
    transition,
)
from ap_agent.tools.extract_invoice_vision import (
    PROMPT_VERSION,
    ExtractInvoiceVisionInput,
    extract_invoice_vision,
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


@app.command()
def extract(
    path: Annotated[Path, typer.Argument(help="Invoice document to read.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Overrides Settings.extraction_model.")
    ] = None,
    prompt_version: Annotated[
        str, typer.Option("--prompt-version", help="Selects prompts/<version>.md.")
    ] = PROMPT_VERSION,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Also write the extraction to this file as JSON."),
    ] = None,
) -> None:
    """Read one invoice with the extraction model and print the result.

    This spends tokens. It is the only command in this CLI that calls an API.

    Nothing is persisted unless ``--out`` is given. Writing an extraction to the
    database is the agent loop's job, and it does so in the same transaction as
    the AuditEvent that records it - a CLI that quietly inserted rows would
    produce invoices with no trail, which is the one thing this system exists to
    prevent.
    """
    try:
        result = extract_invoice_vision(
            ExtractInvoiceVisionInput(path=path, model_id=model, prompt_version=prompt_version)
        )
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    payload = json.dumps(result.extraction.model_dump(mode="json"), indent=2)
    typer.echo(payload)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        typer.secho(f"written to {out}", fg=typer.colors.GREEN, err=True)

    typer.secho(
        f"{result.model_id}  prompt={result.prompt_version}  "
        f"in={result.input_tokens} out={result.output_tokens} tokens  "
        f"{result.latency_ms} ms",
        fg=typer.colors.CYAN,
        err=True,
    )


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
def run_invoice(
    path: Annotated[Path, typer.Argument(help="Invoice document to process.")],
    max_steps: Annotated[
        int, typer.Option("--max-steps", min=1, help="Step budget before escalating.")
    ] = DEFAULT_MAX_STEPS,
    audit_dir: Annotated[
        Path, typer.Option("--audit-dir", help="Where the JSONL trail is written.")
    ] = REPO_ROOT / "data" / "audit",
) -> None:
    """Run one invoice through the loop and print where it stopped.

    This spends tokens: the extraction step calls the API. Most other steps are
    stubs, so a run that reaches CLOSED has proved the pipeline's shape, not
    that an invoice was really matched, approved or posted.
    """
    record = InvoiceRecord(source_path=path, created_at=utc_now())
    writer = JsonlAuditWriter(audit_dir)
    ctx = RunContext(run_id=str(ULID()), writer=writer)

    final = loop_run(record, ctx, max_steps=max_steps)

    colour = typer.colors.GREEN if final.state is InvoiceState.CLOSED else typer.colors.YELLOW
    typer.secho(f"final state : {final.state.value}", fg=colour)
    typer.echo(f"steps       : {len(ctx.events)}")
    typer.echo(f"audit trail : {writer.path_for(str(record.invoice_id))}")
    typer.echo(f"chain intact: {writer.verify(str(record.invoice_id))}")
    if final.validation_flags:
        typer.secho(f"validation  : {', '.join(final.validation_flags)}", fg=typer.colors.YELLOW)


if __name__ == "__main__":  # pragma: no cover
    app()
