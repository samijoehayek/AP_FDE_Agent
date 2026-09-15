"""Ask the API whether it will accept the schemas this system sends it.

Structured outputs enforces a complexity budget that **nothing measurable on
this side predicts**. On 2026-09-12 a change made ``InvoiceExtraction``'s schema
smaller by every count available locally - 104 nodes against 104, the same 19
properties, the same 17 ``anyOf`` branches, fewer bytes - and it was refused with
``400 Schema is too complex``. The cause was one property moving out of
``required``. Two live runs were spent discovering that.

So this exists: the cheapest possible question, asked of the only thing that can
answer it. It sends each exported schema with a two-line dummy document and
reports accepted or refused.

**It spends tokens** - a few hundred input, a handful of output per schema, which
is fractions of a cent. Run it after any change to a contract a model fills in,
before spending a real extraction to find out.

Nothing it sends is a real invoice. The dummy document is two lines of invented
text, so a refusal is about the schema and never about the content.

Usage:
    uv run python scripts/probe_schema.py
    uv run python scripts/probe_schema.py --schema invoice
"""

from __future__ import annotations

import json
import sys
import time
from typing import Annotated, Any

import typer

from ap_agent.config import get_settings
from ap_agent.contracts.exceptions import ExceptionClassification
from ap_agent.contracts.invoice import InvoiceExtraction

app = typer.Typer(add_completion=False, help=__doc__)

DUMMY_DOCUMENT = "ACME SUPPLIES LTD\nInvoice INV-1  Date 01 Jan 2026  Total 10.00 USD"
"""Two invented lines. A refusal here is about the schema, never the content."""

MAX_TOKENS = 1024

SCHEMAS: dict[str, type[Any]] = {
    "invoice": InvoiceExtraction,
    "exception": ExceptionClassification,
}


def _measure(model: type[Any]) -> str:
    """The counts that do *not* predict acceptance, printed anyway.

    Worth showing next to the verdict precisely because they are unreliable: a
    reader comparing two runs of this script needs to see that the numbers moved
    one way and the answer moved the other.
    """
    schema = model.model_json_schema()
    raw = json.dumps(schema)
    return (
        f"{len(schema.get('properties', {})):>3} properties, "
        f"{raw.count('{'):>4} nodes, {len(raw):>6} bytes, "
        f"{raw.count('anyOf'):>3} anyOf"
    )


def probe(name: str, model: type[Any]) -> bool:
    """Send one schema with a dummy document. Returns whether it was accepted."""
    import anthropic  # noqa: PLC0415 - only this script needs the SDK

    settings = get_settings()
    kwargs: dict[str, Any] = {"api_key": settings.anthropic_api_key.get_secret_value()}
    if settings.anthropic_workspace_id:
        kwargs["default_headers"] = {"anthropic-workspace-id": settings.anthropic_workspace_id}
    client = anthropic.Anthropic(**kwargs)

    typer.echo(f"{name:<10} {_measure(model)}")
    started = time.perf_counter()
    try:
        client.messages.parse(
            model=settings.extraction_model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": DUMMY_DOCUMENT}],
            # The same argument the extraction seat passes, so this probes the
            # schema that actually ships rather than a reconstruction of it.
            output_format=model,
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        typer.secho(f"{'':<10} REFUSED after {elapsed} ms: {exc}", fg=typer.colors.RED)
        return False

    elapsed = int((time.perf_counter() - started) * 1000)
    typer.secho(f"{'':<10} ACCEPTED in {elapsed} ms", fg=typer.colors.GREEN)
    return True


@app.command()
def main(
    schema: Annotated[
        str | None,
        typer.Option("--schema", help=f"Probe one of: {', '.join(SCHEMAS)}. Default: all."),
    ] = None,
) -> None:
    """Report whether the API accepts each exported schema."""
    if schema is not None and schema not in SCHEMAS:
        typer.secho(
            f"unknown schema {schema!r}; known: {', '.join(SCHEMAS)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    chosen = {schema: SCHEMAS[schema]} if schema else SCHEMAS
    typer.secho("spends tokens: a few hundred per schema\n", fg=typer.colors.YELLOW)

    refused = [name for name, model in chosen.items() if not probe(name, model)]
    if refused:
        typer.secho(
            f"\n{len(refused)} schema(s) refused: {', '.join(refused)}. "
            f"The counts above do not explain it - compare `required` arrays and "
            f"optional properties against the last version that was accepted.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    typer.secho("\nevery schema accepted", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
