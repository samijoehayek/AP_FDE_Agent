"""Walk ``data/`` and write ``data/index.csv``: one row per document.

The index is what makes a corpus usable. Before it exists you have a pile of
files; after it you can answer the questions that actually shape the pipeline -
how many are scans rather than born-digital PDFs, how blurry the worst of them
are, and how many are byte-identical copies of each other.

That last one matters more than it sounds. Exact-hash duplicates in a corpus
inflate every accuracy number: the same document scored fifty times looks like
fifty passes. They are flagged here so they can be excluded from an eval split.

``data/index.csv`` is git-ignored along with the rest of ``data/``.

Usage:
    uv run python scripts/index_invoices.py
    uv run python scripts/index_invoices.py --no-sharpness   # fast pass
"""

from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated

import typer

from ap_agent.errors import APAgentError
from ap_agent.tools.ingest_document import IngestDocumentInput, ingest_document

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"})

# Annotated as tuple[str, ...] rather than left to infer a Literal union, which
# would make csv.DictWriter reject an ordinary dict[str, str] row.
FIELDNAMES: tuple[str, ...] = (
    "file",
    "source",
    "kind",
    "media_type",
    "page_count",
    "byte_size",
    "sha256",
    "has_text_layer",
    "pages_with_text",
    "min_sharpness",
    "duplicate_of",
    "error",
)

app = typer.Typer(add_completion=False, help=__doc__)


def _source_of(path: Path, root: Path) -> str:
    """Return the corpus a file came from: its directory relative to ``data/``."""
    relative = path.relative_to(root).parent
    return str(relative) if str(relative) != "." else "(root)"


@app.command()
def main(
    data_dir: Annotated[Path, typer.Option("--data-dir", help="Corpus root.")] = REPO_ROOT / "data",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Defaults to <data-dir>/index.csv.")
    ] = None,
    no_sharpness: Annotated[
        bool, typer.Option("--no-sharpness", help="Skip page rendering. Much faster.")
    ] = False,
) -> None:
    """Index every document under ``data_dir``."""
    if not data_dir.is_dir():
        typer.secho(f"no such directory: {data_dir}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    destination = output or (data_dir / "index.csv")
    paths = sorted(
        p for p in data_dir.rglob("*") if p.is_file() and p.suffix.lower() in DOCUMENT_SUFFIXES
    )
    if not paths:
        typer.secho(
            f"no documents under {data_dir}. Run scripts/pull_hf_datasets.py first.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    typer.echo(f"indexing {len(paths)} documents under {data_dir}")

    rows: list[dict[str, str]] = []
    first_seen: dict[str, str] = {}
    by_source: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    duplicate_groups: defaultdict[str, list[str]] = defaultdict(list)
    failures = 0

    with typer.progressbar(paths, label="reading") as progress:
        for path in progress:
            relative = str(path.relative_to(data_dir))
            source = _source_of(path, data_dir)
            by_source[source] += 1

            try:
                result = ingest_document(
                    IngestDocumentInput(path=path, compute_sharpness=not no_sharpness)
                )
            except APAgentError as exc:
                failures += 1
                blank = dict.fromkeys(FIELDNAMES, "")
                rows.append(blank | {"file": relative, "source": source, "error": str(exc)})
                continue

            kind = "born_digital" if result.is_born_digital else "scan"
            by_kind[kind] += 1
            duplicate_groups[result.sha256].append(relative)
            duplicate_of = first_seen.setdefault(result.sha256, relative)

            rows.append(
                {
                    "file": relative,
                    "source": source,
                    "kind": kind,
                    "media_type": result.media_type,
                    "page_count": str(result.page_count),
                    "byte_size": str(result.byte_size),
                    "sha256": result.sha256,
                    "has_text_layer": str(result.has_text_layer),
                    "pages_with_text": str(result.pages_with_text),
                    "min_sharpness": (
                        "" if result.min_sharpness is None else f"{result.min_sharpness:.2f}"
                    ),
                    "duplicate_of": "" if duplicate_of == relative else duplicate_of,
                    "error": "",
                }
            )

    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    duplicates = {h: files for h, files in duplicate_groups.items() if len(files) > 1}
    extra_copies = sum(len(files) - 1 for files in duplicates.values())

    typer.secho(f"\nwrote {destination}", fg=typer.colors.GREEN)
    typer.echo(f"  {len(rows)} documents, {failures} unreadable")
    for kind, count in sorted(by_kind.items()):
        typer.echo(f"  {kind:<14} {count}")
    typer.echo("  by source:")
    for source, count in sorted(by_source.items()):
        typer.echo(f"    {source:<28} {count}")
    if duplicates:
        typer.secho(
            f"  {extra_copies} exact-hash duplicate copies across "
            f"{len(duplicates)} distinct documents - exclude these from any eval split",
            fg=typer.colors.YELLOW,
        )


if __name__ == "__main__":
    sys.exit(app())
