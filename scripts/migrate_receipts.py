"""Add ``line_no`` to every receipt line in ``data/generated/receipts.json``.

A one-off. The file was written before anything joined against it, so its lines
carried an item name and nothing else, and ``get_receipts`` matched them to
purchase-order lines **by position**. That works exactly as long as nobody
reorders a purchase order, and fails silently the moment somebody does - the
quantities would land on the wrong lines and the matcher would report a variance
on a line that was fine.

Position is what the file has, so position is what the numbers come from. The
difference is that afterwards the join key is written down rather than assumed,
and a reordering breaks loudly instead of quietly.

Idempotent: a line that already has a ``line_no`` is left alone. Safe to run
twice, and it changes nothing else in the file.

Usage:
    uv run python scripts/migrate_receipts.py --dry-run
    uv run python scripts/migrate_receipts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from ap_agent.config import get_settings

app = typer.Typer(add_completion=False, help=__doc__)


def add_line_numbers(document: dict[str, Any]) -> int:
    """Number every unnumbered receipt line by its position. Returns how many.

    1-based, to match QuickBooks' own ``LineNum`` - the two have to agree or the
    join this exists to fix does not work.
    """
    added = 0
    raw_receipts: Any = document.get("receipts")
    receipts = cast("list[Any]", raw_receipts) if isinstance(raw_receipts, list) else []
    for raw_receipt in receipts:
        if not isinstance(raw_receipt, dict):
            continue
        receipt = cast("dict[str, Any]", raw_receipt)
        raw_lines: Any = receipt.get("lines")
        lines = cast("list[Any]", raw_lines) if isinstance(raw_lines, list) else []
        for index, raw_line in enumerate(lines, start=1):
            if not isinstance(raw_line, dict):
                continue
            line = cast("dict[str, Any]", raw_line)
            if "line_no" not in line:
                line["line_no"] = index
                added += 1
    return added


@app.command()
def main(
    path: Annotated[
        Path | None, typer.Option("--path", help="Receipts file. Defaults to the configured one.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would change and write nothing.")
    ] = False,
) -> None:
    """Add ``line_no`` to every receipt line that lacks one."""
    source = path or get_settings().receipts_path
    if not source.is_file():
        typer.secho(f"no receipts file at {source}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    document: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    added = add_line_numbers(document)

    if added == 0:
        typer.secho("every receipt line already carries a line_no", fg=typer.colors.GREEN)
        return

    if dry_run:
        typer.secho(f"dry run: would number {added} lines in {source}", fg=typer.colors.YELLOW)
        return

    source.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    typer.secho(f"numbered {added} receipt lines in {source}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
