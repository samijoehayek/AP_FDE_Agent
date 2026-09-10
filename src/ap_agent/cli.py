"""Command-line entry points.

Only commands backed by implemented code are wired up. ``ap-agent run`` exists
and fails loudly rather than being absent, so that the shape of the finished
system is visible from ``--help`` on day one.
"""

from __future__ import annotations

import json
from datetime import date
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
from ap_agent.tools.compute_extraction_confidence import (
    ComputeExtractionConfidenceInput,
    ExtractionConfidence,
    compute_extraction_confidence,
    find_raw_date,
)
from ap_agent.tools.extract_invoice_text import (
    ExtractInvoiceTextInput,
    ExtractInvoiceTextOutput,
    extract_invoice_text,
)
from ap_agent.tools.extract_invoice_vision import (
    PROMPT_VERSION,
    ExtractInvoiceVisionInput,
    ExtractInvoiceVisionOutput,
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


@app.command()
def confidence(
    path: Annotated[Path, typer.Argument(help="Invoice document to read twice.")],
    country: Annotated[
        str | None,
        typer.Option(
            "--country",
            "-c",
            help="Vendor's ISO-3166-1 alpha-2 country. Settles DD/MM against MM/DD.",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Also write the full verdict to this file as JSON."),
    ] = None,
) -> None:
    """Read one invoice twice and report how much of it can be trusted.

    Two model calls: the vision model over the page image, then the cheaper text
    model over the PDF's own characters. Where they agree *and* the value is
    present in the text layer, the field passes; anything else is named in
    ``needs_human``.

    This spends tokens, roughly the cost of ``extract`` plus a few cents.

    Nothing is persisted unless ``--out`` is given, and nothing is wired into the
    agent loop yet - this command exists so the check can be run against real
    invoices and its thresholds argued with before it decides anything.
    """
    try:
        document = ingest_document(IngestDocumentInput(path=path))
        vision = extract_invoice_vision(ExtractInvoiceVisionInput(path=path))
        text = extract_invoice_text(ExtractInvoiceTextInput(path=path))
        verdict = compute_extraction_confidence(
            ComputeExtractionConfidenceInput(
                primary=vision.extraction,
                secondary=text.second_read,
                raw_text=text.raw_text,
                vendor_country=country,
                min_sharpness=document.min_sharpness,
            )
        ).confidence
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _print_confidence(verdict, vision, text)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(verdict.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        typer.secho(f"written to {out}", fg=typer.colors.GREEN, err=True)

    raise typer.Exit(code=0 if verdict.auto_ok else 2)


def _print_confidence(
    verdict: ExtractionConfidence,
    vision: ExtractInvoiceVisionOutput,
    text: ExtractInvoiceTextOutput,
) -> None:
    """Render the verdict as a table. Two colours: passed, or did not."""
    primary = vision.extraction
    typer.echo(f"{'field':<16}{'primary':<34}{'agreed':<8}{'grounded':<10}{'score':<7}reason")
    typer.echo("-" * 100)
    for entry in verdict.fields:
        value = str(getattr(primary, entry.field))
        passed = entry.agreed is True and entry.grounded
        typer.secho(
            f"{entry.field:<16}{value[:32]:<34}"
            f"{_tick(entry.agreed):<8}{_tick(entry.grounded):<10}"
            f"{entry.score:<7.2f}{entry.reason}",
            fg=typer.colors.GREEN if passed else typer.colors.YELLOW,
        )

    _print_date_note(verdict, primary.invoice_date, text.raw_text)

    typer.echo("")
    if verdict.auto_ok:
        typer.secho("auto_ok: yes", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho(
            f"auto_ok: no  needs_human: {', '.join(verdict.needs_human)}",
            fg=typer.colors.RED,
            bold=True,
        )

    typer.secho(
        f"\nvision {vision.model_id} in={vision.input_tokens} out={vision.output_tokens}"
        f"  |  text {text.model_id} in={text.input_tokens} out={text.output_tokens}"
        f"  |  text layer {len(text.raw_text)} chars",
        fg=typer.colors.CYAN,
        err=True,
    )


def _print_date_note(verdict: ExtractionConfidence, extracted: date, raw_text: str) -> None:
    """Explain an ambiguous date in the terms the page states it.

    ``-> None`` is not an explanation. Whoever is reading this needs to see the
    digits that are ambiguous and both ways they can be read, because they are
    the one who has to decide which vendor this is.
    """
    if verdict.resolved_invoice_date == extracted:
        return

    raw = find_raw_date(raw_text, extracted)
    rendering = f"the page reads {raw.text}" if raw else "the page is ambiguous"

    if verdict.resolved_invoice_date is None:
        readings = ""
        if raw is not None:
            day_first = raw.resolve(day_first=True)
            month_first = raw.resolve(day_first=False)
            readings = f", which is {day_first} day-first or {month_first} month-first"
        typer.secho(
            f"\ninvoice_date unresolved: {rendering}{readings}. Pass --country to settle it.",
            fg=typer.colors.MAGENTA,
        )
        return

    typer.secho(
        f"\ninvoice_date reinterpreted: {rendering}, read as {extracted} and "
        f"resolved to {verdict.resolved_invoice_date}",
        fg=typer.colors.MAGENTA,
    )


def _tick(value: bool | None) -> str:
    """A tri-state as one character: unknown is not the same as false."""
    return {True: "yes", False: "NO", None: "?"}[value]


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
