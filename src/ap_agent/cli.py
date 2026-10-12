"""Command-line entry points.

Only commands backed by implemented code are wired up. ``ap-agent run`` exists
and fails loudly rather than being absent, so that the shape of the finished
system is visible from ``--help`` on day one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from pydantic import ValidationError
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker
from ulid import ULID

from ap_agent import __version__
from ap_agent.audit.writer import JsonlAuditWriter
from ap_agent.config import REPO_ROOT
from ap_agent.contracts.audit import AuditEvent, utc_now
from ap_agent.contracts.run import InvoiceRecord
from ap_agent.db.base import create_db_engine, create_session_factory
from ap_agent.db.store import PostgresApprovalStore, PostgresDedupIndex, PostgresInvoiceStore
from ap_agent.errors import APAgentError, HumanDecisionError
from ap_agent.loop import durable
from ap_agent.loop.dates import resolve_date_by_receipt_window
from ap_agent.loop.runner import (
    DEFAULT_MAX_STEPS,
    RunContext,
    apply_human_decision,
    invoice_max_age_days,
)
from ap_agent.payloads import JSON, PayloadStore, PostgresPayloadStore, parse_ref
from ap_agent.persistence import ApprovalStore, DedupIndex, InvoiceStore, UnknownInvoiceError
from ap_agent.states.machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    InvoiceEvent,
    InvoiceState,
    allowed_events,
    is_terminal,
    to_mermaid,
    transition,
)
from ap_agent.tools.compute_extraction_confidence import (
    ComputeExtractionConfidenceInput,
    DateResolutionReason,
    DateVerdict,
    ExtractionConfidence,
    compute_extraction_confidence,
)
from ap_agent.tools.extract_invoice_text import (
    ExtractInvoiceTextInput,
    extract_invoice_text,
)
from ap_agent.tools.extract_invoice_vision import (
    PROMPT_VERSION,
    ExtractInvoiceVisionInput,
    extract_invoice_vision,
)
from ap_agent.tools.get_purchase_order import GetPurchaseOrderInput, get_purchase_order
from ap_agent.tools.get_receipts import GetReceiptsInput, get_receipts
from ap_agent.tools.ingest_document import IngestDocumentInput, ingest_document
from ap_agent.tools.lookup_vendor import LookupVendorInput, lookup_vendor

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
    received_at: Annotated[
        datetime | None,
        typer.Option(
            "--received-at",
            formats=["%Y-%m-%d"],
            help="The day the document arrived. Settles an ambiguous date when only one "
            "reading could have been received by then.",
        ),
    ] = None,
    from_extraction: Annotated[
        Path | None,
        typer.Option(
            "--from-extraction",
            help="Replay the check against readings saved by an earlier --out run. Makes no "
            "model calls at all.",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write the readings and the verdict to this file."),
    ] = None,
) -> None:
    """Read one invoice twice and report how much of it can be trusted.

    Two model calls: the vision model over the page image, then the cheaper text
    model over the PDF's own characters. Where they agree *and* the value is
    present in the text layer, the field passes; anything else is named in
    ``needs_human``, with the field to open the document for.

    This spends tokens, roughly the cost of ``extract`` plus a few cents - unless
    ``--from-extraction`` is given, which replays saved readings and calls
    nothing. Use it whenever the question is about the *check* rather than about
    the models: the thresholds here will be argued with many more times than the
    readings will change.

    Exits 2 when a person is needed, so it composes with a shell.
    """
    try:
        readings = (
            _load_readings(from_extraction, country)
            if from_extraction is not None
            else _read_twice(path, country)
        )
        verdict = compute_extraction_confidence(readings).confidence
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    received = received_at.date() if received_at is not None else None
    _print_confidence(verdict, readings, received)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "readings": readings.model_dump(mode="json"),
            "confidence": verdict.model_dump(mode="json"),
        }
        out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        typer.secho(f"written to {out}", fg=typer.colors.GREEN, err=True)

    raise typer.Exit(code=0 if verdict.auto_ok else 2)


def _read_twice(path: Path, country: str | None) -> ComputeExtractionConfidenceInput:
    """Ingest, read the page image, read the text layer. Two model calls."""
    document = ingest_document(IngestDocumentInput(path=path))
    vision = extract_invoice_vision(ExtractInvoiceVisionInput(path=path))
    text = extract_invoice_text(ExtractInvoiceTextInput(path=path))
    typer.secho(
        f"vision {vision.model_id} in={vision.input_tokens} out={vision.output_tokens}"
        f"  |  text {text.model_id} in={text.input_tokens} out={text.output_tokens}"
        f"  |  text layer {len(text.raw_text)} chars",
        fg=typer.colors.CYAN,
        err=True,
    )
    return ComputeExtractionConfidenceInput(
        primary=vision.extraction,
        secondary=text.second_read,
        raw_text=text.raw_text,
        vendor_country=country,
        min_sharpness=document.min_sharpness,
    )


def _load_readings(source: Path, country: str | None) -> ComputeExtractionConfidenceInput:
    """Rebuild the check's inputs from a file an earlier ``--out`` run wrote.

    Accepts the whole ``{readings, confidence}`` document or a bare readings
    object, so a file can be hand-trimmed without the command rejecting it.
    ``--country`` still overrides what was saved: the country is the one input
    that is not a property of the document.
    """
    try:
        parsed: object = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"could not read saved readings from {source}: {exc}"
        raise APAgentError(msg) from exc

    if not isinstance(parsed, dict):
        msg = f"{source} does not contain a JSON object"
        raise APAgentError(msg)

    payload = cast("dict[str, object]", parsed)
    nested = payload.get("readings")
    body = cast("dict[str, object]", nested) if isinstance(nested, dict) else payload
    try:
        readings = ComputeExtractionConfidenceInput.model_validate(body)
    except ValidationError as exc:
        msg = f"{source} is not a saved readings file: {exc}"
        raise APAgentError(msg) from exc

    return readings if country is None else readings.model_copy(update={"vendor_country": country})


def _print_confidence(
    verdict: ExtractionConfidence,
    readings: ComputeExtractionConfidenceInput,
    received_at: date | None,
) -> None:
    """Render the verdict as a table. Two colours: passed, or did not."""
    primary = readings.primary
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

    _print_date_note(verdict, primary.invoice_date, received_at)

    typer.echo("")
    if verdict.auto_ok:
        typer.secho("auto_ok: yes", fg=typer.colors.GREEN, bold=True)
    else:
        blocking = ", ".join(f"{item.field} ({item.reason})" for item in verdict.needs_human)
        typer.secho(f"auto_ok: no  needs_human: {blocking}", fg=typer.colors.RED, bold=True)


def _print_date_note(
    verdict: ExtractionConfidence, extracted: date, received_at: date | None
) -> None:
    """Say what happened to the invoice date, in the terms the page states it.

    ``-> None`` is not an explanation. Whoever reads this is the person who has
    to decide, so they get the digits, both readings, and what would settle it.
    """
    if verdict.date_verdict is DateVerdict.UNAMBIGUOUS:
        return

    rendering = f"the page reads {verdict.date_raw_text}" if verdict.date_raw_text else "the page"

    if verdict.date_verdict is DateVerdict.RESOLVED:
        typer.secho(
            f"\ninvoice_date resolved: read as {extracted}, resolved to "
            f"{verdict.resolved_invoice_date} ({_reason_text(verdict)})",
            fg=typer.colors.MAGENTA,
        )
        return

    candidates = " or ".join(day.isoformat() for day in verdict.date_candidates)
    typer.secho(
        f"\ninvoice_date ambiguous: {rendering}, which is {candidates}.",
        fg=typer.colors.MAGENTA,
    )

    if received_at is None:
        typer.secho(
            "  not a failure - the loop carries both readings forward. "
            "Pass --country or --received-at to settle it here.",
            fg=typer.colors.MAGENTA,
        )
        return

    chosen = resolve_date_by_receipt_window(
        verdict.date_candidates, received_at, invoice_max_age_days()
    )
    if chosen is None:
        typer.secho(
            f"  both readings survive a {received_at} receipt date; still open.",
            fg=typer.colors.MAGENTA,
        )
    else:
        typer.secho(
            f"  received {received_at} -> {chosen} ({DateResolutionReason.RECEIPT_WINDOW.value})",
            fg=typer.colors.MAGENTA,
        )


def _reason_text(verdict: ExtractionConfidence) -> str:
    return verdict.date_resolution_reason.value if verdict.date_resolution_reason else "resolved"


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


@app.command()
def vendor(
    name: Annotated[str, typer.Argument(help="Vendor name as printed on the invoice.")],
    tax_id: Annotated[
        str | None, typer.Option("--tax-id", help="Tax identifier, if the document carried one.")
    ] = None,
) -> None:
    """Resolve a vendor against config/sandbox_vendor_master.yaml.

    Reads a committed file and calls nothing. Use it to see why a name did or
    did not resolve before spending a model call on the whole loop.
    """
    try:
        match = lookup_vendor(LookupVendorInput(vendor_name=name, vendor_tax_id=tax_id)).match
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(match.model_dump_json(indent=2))
    if not match.resolved:
        typer.secho(
            "\nno match: this invoice would route to NEW_VENDOR and wait for a person.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def receipts(
    po_number: Annotated[str, typer.Argument(help="Purchase-order number, e.g. AP-SEED-001.")],
) -> None:
    """Print what was received against a purchase order.

    Reads ``data/generated/receipts.json`` and calls nothing. An unknown order
    prints an empty line list, which is the honest answer: nothing arrived.
    """
    try:
        found = get_receipts(GetReceiptsInput(po_number=po_number)).receipts
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(found.model_dump_json(indent=2))
    if found.is_empty:
        typer.secho(
            "\nnothing received: an invoice against this order is an exception however "
            "clean its arithmetic.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def po(
    po_number: Annotated[str, typer.Argument(help="Purchase-order number, e.g. AP-SEED-001.")],
) -> None:
    """Fetch one purchase order from QuickBooks and print it.

    **This makes a live call.** It is the only command here that does: `vendor`
    reads a committed config file and `receipts` reads a local one. It needs
    working QuickBooks credentials in .env and spends no tokens.
    """
    try:
        found = get_purchase_order(GetPurchaseOrderInput(po_number=po_number)).purchase_order
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if found is None:
        typer.secho(f"no purchase order {po_number} in QuickBooks", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    typer.echo(found.model_dump_json(indent=2))


def _sessions() -> sessionmaker[Session]:
    """Sessions as the application role.

    ``NullPool``: a CLI command is one short process, and a pool would hold
    connections open past its own lifetime.
    """
    return create_session_factory(create_db_engine(poolclass=NullPool))


def payload_store() -> PayloadStore:
    """The payload store ``run`` and ``payload`` use: Postgres.

    Functions rather than constants, here and below, so a test can substitute an
    in-memory store without a database.
    """
    return PostgresPayloadStore(_sessions())


def invoice_store(payloads: PayloadStore) -> InvoiceStore:
    """The invoice store ``run`` uses: Postgres, records as payloads."""
    return PostgresInvoiceStore(_sessions(), payloads)


def dedup_index() -> DedupIndex:
    """The duplicate index ``run`` uses: Postgres."""
    return PostgresDedupIndex(_sessions())


def approval_store() -> ApprovalStore:
    """The approval store ``run`` and ``decide`` use: Postgres."""
    return PostgresApprovalStore(_sessions())


def run_durably(invoice_id: str, ctx: RunContext, max_steps: int) -> None:
    """How ``run``, ``decide`` and ``recover`` drive the loop: as a DBOS workflow.

    A function rather than a direct call so a test without a database can run
    the loop in process instead.
    """
    durable.execute(invoice_id, ctx, max_steps)


def workflow_status(run_id: str) -> str | None:
    """DBOS's status for a run. Substituted alongside :func:`run_durably`."""
    return durable.workflow_status(run_id)


def _choose_run(store: InvoiceStore, invoice_id: str) -> tuple[str, bool]:
    """The run to drive this invoice: the unfinished one if DBOS can finish it, else a new one.

    Returns ``(run_id, recovering)``. A run left unfinished whose workflow DBOS
    no longer drives - it never started, or it ended without closing its row -
    is closed as ``abandoned``: the invoices table still says where the invoice
    is, so a new run starts from there and loses nothing.
    """
    unfinished = store.unfinished_run(invoice_id)
    if unfinished is not None:
        status = workflow_status(unfinished)
        if status in durable.UNFINISHED:
            return unfinished, True
        store.abandon_run(unfinished)
    return str(ULID()), False


def _trail_rows(writer: JsonlAuditWriter, invoice_id: str, run_id: str) -> list[AuditEvent]:
    """This run's rows, from the trail: a recovered run's earlier rows were written elsewhere."""
    return [event for event in writer.read(invoice_id) if event.run_id == run_id]


def _busy(invoice_id: str) -> typer.Exit:
    typer.secho(
        f"busy        : invoice {invoice_id} is being run by another process",
        fg=typer.colors.RED,
        err=True,
    )
    return typer.Exit(code=1)


@app.command()
def payload(
    ref: Annotated[str, typer.Argument(help="A sha256, bare or as sha256:<hex>.")],
) -> None:
    """Print one stored payload: whatever an audit row's output_ref points at.

    Reads Postgres as the application role. JSON is pretty-printed; anything
    else is printed as stored. Exits 1 if the reference is malformed or nothing
    is stored under it - a reference written before payloads were stored, for
    instance, which is honest about not resolving.
    """
    try:
        digest = parse_ref(ref)
    except ValueError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    stored = payload_store().get(digest)
    if stored is None:
        typer.secho(f"no payload stored under sha256:{digest}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)

    typer.secho(f"# sha256:{stored.sha256}  {stored.media_type}", fg=typer.colors.BLUE, err=True)
    text = stored.content.decode("utf-8", errors="replace")
    if stored.media_type == JSON:
        text = json.dumps(json.loads(text), indent=2)
    typer.echo(text)


@app.command(name="run")
def run_invoice(
    path: Annotated[Path, typer.Argument(help="Invoice document to process.")],
    max_steps: Annotated[
        int, typer.Option("--max-steps", min=1, help="Step budget before escalating.")
    ] = DEFAULT_MAX_STEPS,
    received_at: Annotated[
        datetime | None,
        typer.Option(
            "--received-at",
            formats=["%Y-%m-%d"],
            help="The day the document arrived. Lets the loop settle an ambiguous date.",
        ),
    ] = None,
    audit_dir: Annotated[
        Path, typer.Option("--audit-dir", help="Where the JSONL trail is written.")
    ] = REPO_ROOT / "data" / "audit",
) -> None:
    """Run one invoice through the loop - or resume it - and print where it stopped.

    **Keyed by the document's sha256.** A file that has never arrived starts a
    new invoice. A file that has arrived before resumes its invoice from the
    state stored for it: a halted one carries on (or halts again, if it is still
    waiting for a person), one interrupted mid-step restarts that step. A file
    whose invoice is CLOSED, REJECTED or CANCELLED is reported and left alone.

    This spends tokens when it reaches the extraction step. Most steps after the
    match are stubs, so a run that reaches CLOSED has proved the pipeline's
    shape, not that an invoice was really approved or posted.

    ``--received-at`` is the caller's to supply, and only on the first run: the
    loop never reads a clock to decide when a document arrived.
    """
    if not path.is_file():
        typer.secho(f"error: not a file: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    content = path.read_bytes()
    if not content:
        typer.secho(f"error: empty file: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    document_sha256 = hashlib.sha256(content).hexdigest()

    payloads = payload_store()
    store = invoice_store(payloads)
    existing = store.find_by_document(document_sha256)

    if existing is not None and is_terminal(existing.state):
        typer.secho(
            f"already {existing.state.value}: invoice {existing.invoice_id}. Nothing to do.",
            fg=typer.colors.YELLOW,
        )
        return

    if existing is not None:
        typer.echo(f"resuming    : invoice {existing.invoice_id} from {existing.state.value}")
        if received_at is not None:
            typer.secho(
                "received-at : ignored on resume - the first run's arrival date stands",
                fg=typer.colors.YELLOW,
            )
        record = existing.model_copy(update={"source_path": path})
        store.save(record)  # the file may have moved; later steps read it from here
    else:
        record = InvoiceRecord(
            source_path=path,
            created_at=utc_now(),
            received_at=received_at.replace(tzinfo=UTC) if received_at else None,
        )
        store.create(
            record,
            document_sha256=document_sha256,
            byte_size=len(content),
            storage_uri=path.resolve().as_uri(),
        )

    invoice_id = str(record.invoice_id)
    writer = JsonlAuditWriter(audit_dir)
    with store.lock(invoice_id) as held:
        if not held:
            raise _busy(invoice_id)
        run_id, recovering = _choose_run(store, invoice_id)
        if recovering:
            typer.echo(f"recovering  : run {run_id}, left unfinished by a process that stopped")
        ctx = RunContext(
            run_id=run_id,
            writer=writer,
            payloads=payloads,
            store=store,
            dedup=dedup_index(),
            approvals=approval_store(),
        )
        run_durably(invoice_id, ctx, max_steps)
    # Printed from the store and the trail, not from this process's memory: what
    # is shown is what a fresh process resuming this invoice would see.
    final = store.load(invoice_id)
    rows = _trail_rows(writer, invoice_id, run_id)

    moves = [event for event in rows if event.to_state is not None]
    colour = typer.colors.GREEN if final.state is InvoiceState.CLOSED else typer.colors.YELLOW
    typer.secho(f"final state : {final.state.value}", fg=colour)
    typer.echo(f"steps       : {len(moves)}")
    typer.echo(f"audit rows  : {len(rows)}")
    typer.echo(f"audit trail : {writer.path_for(invoice_id)}")
    typer.echo(f"chain intact: {writer.verify(invoice_id)}")
    _print_bill(final)
    _print_run_date(final)
    _print_staleness(final)
    if final.validation_flags:
        typer.secho(f"validation  : {', '.join(final.validation_flags)}", fg=typer.colors.YELLOW)
    _print_explanation(final)
    _print_approval(final, ctx.approvals)


def _print_approval(final: InvoiceRecord, approvals: ApprovalStore) -> None:
    """At PENDING_APPROVAL: who must approve, how many have, and how to do it.

    This is ``notify``'s other half for now - the structured log event goes to
    whatever reads logs, and this goes to the person at the terminal.
    """
    if final.state is not InvoiceState.PENDING_APPROVAL or final.approval_id is None:
        return
    view = approvals.get(final.approval_id)
    if view is None:
        return
    typer.echo(
        f"approval    : {len(view.approvals)} of {view.required_approvers} "
        f"({view.approver_role}), request {view.approval_id}"
    )
    typer.secho(
        f'to decide   : just decide {final.invoice_id} approve|reject --by <you> --reason "<why>"',
        fg=typer.colors.YELLOW,
    )


DECIDE_ACTIONS: dict[str, InvoiceEvent] = {
    "approve": InvoiceEvent.APPROVE,
    "reject": InvoiceEvent.REJECT,
    "cancel": InvoiceEvent.CANCEL,
    "rematch": InvoiceEvent.REMATCH,
    "accept": InvoiceEvent.ACCEPT_WITH_REASON,
    "confirm-duplicate": InvoiceEvent.CONFIRM_DUPLICATE,
    "not-duplicate": InvoiceEvent.NOT_DUPLICATE,
}


@app.command()
def decide(
    invoice_id: Annotated[str, typer.Argument(help="The invoice, as `just run` printed it.")],
    action: Annotated[str, typer.Argument(help=f"One of: {', '.join(DECIDE_ACTIONS)}.")],
    by: Annotated[str, typer.Option("--by", help="Who is deciding. Recorded on the row.")],
    reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            help="Why. Required to approve, reject an approval, accept "
            "an exception, or clear a duplicate.",
        ),
    ] = None,
    audit_dir: Annotated[
        Path, typer.Option("--audit-dir", help="Where the JSONL trail is written.")
    ] = REPO_ROOT / "data" / "audit",
) -> None:
    """Record a person's decision on a waiting invoice, then carry on from it.

    The only way a person's decision enters the system: the row's actor is
    ``--by``. A vote that does not complete an approval is recorded and the
    invoice keeps waiting. Anything that moves the invoice is followed by a run,
    so an approved invoice goes on as far as it can: POST@v1 makes or finds its
    bill in QuickBooks, and the stubs after POSTED take it to CLOSED.
    """
    event = DECIDE_ACTIONS.get(action)
    if event is None:
        typer.secho(
            f"error: {action!r} is not one of {', '.join(DECIDE_ACTIONS)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    payloads = payload_store()
    store = invoice_store(payloads)
    try:
        record = store.load(invoice_id)
    except UnknownInvoiceError as exc:
        typer.secho(f"error: no invoice {invoice_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    writer = JsonlAuditWriter(audit_dir)
    with store.lock(invoice_id) as held:
        if not held:
            raise _busy(invoice_id)
        unfinished = store.unfinished_run(invoice_id)
        if unfinished is not None:
            typer.secho(
                f"refused     : run {unfinished} never finished on this invoice; "
                "`just run <document>` or `just recover` finishes it first",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        ctx = RunContext(
            run_id=str(ULID()),
            writer=writer,
            payloads=payloads,
            store=store,
            dedup=dedup_index(),
            approvals=approval_store(),
        )
        try:
            decided = apply_human_decision(record, ctx, event=event, user_id=by, reason=reason)
        except HumanDecisionError as exc:
            typer.secho(f"refused     : {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

        if decided.state is record.state:
            typer.echo(f"recorded    : {ctx.events[-1].decision_basis}")
            _print_approval(decided, ctx.approvals)
            return

        typer.echo(f"decided     : {record.state.value} -> {decided.state.value}")
        if not is_terminal(decided.state):
            # The same run id: the person's row and the rows that follow it are
            # one run on the trail.
            run_durably(invoice_id, ctx, DEFAULT_MAX_STEPS)
    final = store.load(invoice_id)
    colour = typer.colors.GREEN if final.state is InvoiceState.CLOSED else typer.colors.YELLOW
    typer.secho(f"final state : {final.state.value}", fg=colour)
    typer.echo(f"audit trail : {writer.path_for(invoice_id)}")
    typer.echo(f"chain intact: {writer.verify(invoice_id)}")
    _print_bill(final)
    _print_explanation(final)
    _print_approval(final, ctx.approvals)


@app.command()
def recover(
    audit_dir: Annotated[
        Path, typer.Option("--audit-dir", help="Where the JSONL trail is written.")
    ] = REPO_ROOT / "data" / "audit",
) -> None:
    """Finish every run a stopped process left unfinished, one invoice at a time.

    The explicit form of what ``just run <document>`` does for one document. A
    run another live process is driving is skipped - its invoice's lock is
    taken - so this can never become a second process running the same steps.
    """
    payloads = payload_store()
    store = invoice_store(payloads)
    pending = store.unfinished_runs()
    if not pending:
        typer.echo("nothing to recover")
        return
    writer = JsonlAuditWriter(audit_dir)
    for run_id, invoice_id in pending:
        with store.lock(invoice_id) as held:
            if not held:
                typer.secho(f"skipped     : {invoice_id} (run {run_id}) is live elsewhere")
                continue
            if workflow_status(run_id) not in durable.UNFINISHED:
                store.abandon_run(run_id)
                typer.secho(f"abandoned   : run {run_id} on {invoice_id}; DBOS cannot finish it")
                continue
            ctx = RunContext(
                run_id=run_id,
                writer=writer,
                payloads=payloads,
                store=store,
                dedup=dedup_index(),
                approvals=approval_store(),
            )
            run_durably(invoice_id, ctx, DEFAULT_MAX_STEPS)
        final = store.load(invoice_id)
        typer.echo(f"recovered   : run {run_id} on {invoice_id} -> {final.state.value}")


def _print_bill(final: InvoiceRecord) -> None:
    """The QuickBooks bill this invoice became, once POST@v1 has made or found it."""
    if final.erp_bill_id is not None:
        typer.echo(f"erp bill    : {final.erp_bill_id} (key {final.erp_idempotency_key})")


def _print_explanation(final: InvoiceRecord) -> None:
    """Show what the person at PENDING_HUMAN will read.

    The trail keeps the classification's hash, not its words - prose belongs on
    the record - and the record shown here is the one *stored*, so a resumed run
    prints the summary the original run wrote. Labelled as model-written, as the
    architecture asks of anywhere it is displayed.
    """
    if final.state is not InvoiceState.PENDING_HUMAN:
        return
    explained = final.classification
    if explained is None:
        typer.secho(
            f"explanation : none ({final.classification_rejected or 'not recorded'})",
            fg=typer.colors.YELLOW,
        )
        return
    typer.echo(
        f"explanation : lead={explained.reason_code.value}, "
        f"resolver={explained.suggested_resolver.value}, "
        f"action={explained.suggested_action.value}"
    )
    typer.echo(f"summary     : [AI-generated] {explained.human_summary}")


def _print_staleness(final: InvoiceRecord) -> None:
    """Say when the age check did not run, so a pass is not read as a check.

    The staleness rule measures an invoice's age against the day it *arrived*,
    and the loop never invents an arrival date - a receipt date it made up would
    be evidence about itself. So with no ``--received-at`` the check is skipped
    entirely, and a run that says nothing would look exactly like a run that
    checked and was satisfied.
    """
    if final.received_at is None:
        typer.secho(
            "staleness   : SKIPPED - no --received-at, so the invoice's age was never "
            "checked. Pass --received-at YYYY-MM-DD to run it.",
            fg=typer.colors.YELLOW,
        )
        return
    typer.echo(f"staleness   : checked against received_at={final.received_at.date().isoformat()}")


def _print_run_date(final: InvoiceRecord) -> None:
    """Say what the invoice date ended up as, and whether anything had to decide it."""
    if final.date_is_open:
        candidates = " or ".join(day.isoformat() for day in final.date_candidates)
        typer.secho(
            f"invoice date: still open - {candidates}. Pass --received-at, or wait for "
            "lookup_vendor to supply the country.",
            fg=typer.colors.YELLOW,
        )
        return
    if final.date_resolution_reason is not None:
        typer.secho(
            f"invoice date: {final.invoice_date_resolved} ({final.date_resolution_reason.value})",
            fg=typer.colors.MAGENTA,
        )


if __name__ == "__main__":  # pragma: no cover
    app()
