"""Stream sample invoice images from Hugging Face into ``data/synthetic/``.

Two rules this script exists to enforce, both of which are easier to get right
in a downloader than in a policy document:

**Licences are surfaced before anything downloads.** Each dataset's licence is
read from its card via the HF API and printed. Nothing is written to disk unless
``--accept-licenses`` is passed, and a dataset with no declared licence is
called out explicitly - "no licence" is not "permissive", it means nobody has
granted you anything, and a portfolio project that redistributes such data has a
problem a reviewer will notice.

**Nothing lands in git.** ``data/`` is git-ignored in its entirety. These are
third-party corpora and some scanned-document sets contain real names and real
amounts.

Usage:
    uv run python scripts/pull_hf_datasets.py --accept-licenses
    uv run python scripts/pull_hf_datasets.py --limit 10 --dataset mychen76 --accept-licenses
"""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer

REPO_ROOT = Path(__file__).resolve().parents[1]
HF_API = "https://huggingface.co/api/datasets"

app = typer.Typer(add_completion=False, help=__doc__)


@dataclass(frozen=True)
class DatasetSpec:
    """One corpus to pull."""

    key: str
    repo_id: str
    split: str
    default_limit: int
    dest: Path
    note: str


SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        key="mychen76",
        repo_id="mychen76/invoices-and-receipts_ocr_v1",
        split="train",
        default_limit=50,
        dest=REPO_ROOT / "data" / "synthetic" / "hf_mychen76",
        note="Invoice/receipt images with OCR ground truth. Mixed quality; useful for "
        "exercising the vision path and the sharpness triage.",
    ),
    DatasetSpec(
        key="rvlcdip",
        repo_id="chainyo/rvl-cdip-invoice",
        split="train",
        default_limit=20,
        dest=REPO_ROOT / "data" / "synthetic" / "hf_rvlcdip",
        note="The invoice class of RVL-CDIP: low-resolution greyscale scans of real "
        "documents. Derived from the IIT-CDIP tobacco litigation corpus - check the "
        "terms before using it for anything but local evaluation.",
    ),
)


def fetch_license(repo_id: str, *, timeout: float = 20.0) -> str:
    """Return the licence declared on a dataset's card, or a clear 'unknown'."""
    try:
        response = httpx.get(f"{HF_API}/{repo_id}", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return f"UNAVAILABLE (could not reach the HF API: {exc})"

    payload: dict[str, Any] = response.json()
    card: dict[str, Any] = payload.get("cardData") or {}
    declared: object = card.get("license")
    if isinstance(declared, list):
        return ", ".join(str(item) for item in cast("list[object]", declared))
    if declared:
        return str(declared)

    raw_tags: list[Any] = payload.get("tags") or []
    tags = [str(tag) for tag in raw_tags if str(tag).startswith("license:")]
    if tags:
        return ", ".join(tag.removeprefix("license:") for tag in tags)
    return "NOT DECLARED"


def find_image_column(row: dict[str, Any]) -> str | None:
    """Return the name of the column holding a PIL image.

    Detected by duck-typing rather than by a hard-coded column name: these
    corpora use ``image``, ``img``, and ``page_image`` interchangeably, and a
    downloader that breaks when a dataset is re-uploaded with a renamed column
    is a downloader nobody runs twice.
    """
    preferred = ("image", "img", "page_image", "document_image")
    for name in preferred:
        value = row.get(name)
        if hasattr(value, "save") and hasattr(value, "convert"):
            return name
    for name, value in row.items():
        if hasattr(value, "save") and hasattr(value, "convert"):
            return name
    return None


def _write_png(image: Any, destination: Path) -> int:  # noqa: ANN401 - a PIL.Image from an untyped lib
    """Write a PIL image to ``destination`` as PNG and return the byte count."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    payload = buffer.getvalue()
    destination.write_bytes(payload)
    return len(payload)


def pull(spec: DatasetSpec, limit: int) -> int:
    """Stream ``limit`` rows of ``spec`` to disk as PNGs. Returns rows written."""
    # Heavy import, and only needed on the download path. `datasets` types its
    # own signatures only partially, so it is confined to this one call and the
    # result is narrowed to the shape this script actually uses.
    from datasets import (  # noqa: PLC0415
        load_dataset,  # pyright: ignore[reportUnknownVariableType, reportMissingTypeStubs]
    )

    spec.dest.mkdir(parents=True, exist_ok=True)
    typer.echo(f"  streaming {spec.repo_id} [{spec.split}] -> {spec.dest.relative_to(REPO_ROOT)}")

    loader = cast("Callable[..., Iterable[dict[str, Any]]]", load_dataset)
    dataset = loader(spec.repo_id, split=spec.split, streaming=True)
    written = 0
    manifest: list[dict[str, object]] = []

    for index, raw in enumerate(dataset):
        if written >= limit:
            break
        row: dict[str, Any] = dict(raw)
        column = find_image_column(row)
        if column is None:
            if index == 0:
                typer.secho(
                    f"  no image column found; row keys were {sorted(row)}",
                    fg=typer.colors.RED,
                    err=True,
                )
            continue

        out_path = spec.dest / f"{spec.key}_{written:05d}.png"
        size = _write_png(row[column], out_path)
        manifest.append(
            {
                "file": out_path.name,
                "source_repo": spec.repo_id,
                "source_split": spec.split,
                "source_row": index,
                "image_column": column,
                "byte_size": size,
            }
        )
        written += 1

    (spec.dest / "MANIFEST.json").write_text(
        json.dumps(
            {"repo_id": spec.repo_id, "split": spec.split, "rows": manifest},
            indent=2,
        )
    )
    typer.echo(f"  wrote {written} images")
    return written


@app.command()
def main(
    accept_licenses: Annotated[
        bool,
        typer.Option(
            "--accept-licenses",
            help="Confirm you have read each dataset's licence. Required before any download.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="Rows per dataset. Overrides the defaults."),
    ] = None,
    dataset: Annotated[
        str | None,
        typer.Option("--dataset", "-d", help="Pull only this dataset key (mychen76, rvlcdip)."),
    ] = None,
) -> None:
    """Print licences, then stream sample invoice images into ``data/synthetic/``."""
    specs = [s for s in SPECS if dataset is None or s.key == dataset]
    if not specs:
        typer.secho(
            f"unknown dataset {dataset!r}; known: {[s.key for s in SPECS]}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    typer.secho("Dataset licences", bold=True)
    undeclared: list[str] = []
    for spec in specs:
        license_text = fetch_license(spec.repo_id)
        colour = (
            typer.colors.RED
            if license_text.startswith(("NOT DECLARED", "UNAVAILABLE"))
            else typer.colors.GREEN
        )
        typer.echo(f"\n  {spec.repo_id}")
        typer.echo("    licence: ", nl=False)
        typer.secho(license_text, fg=colour)
        typer.echo(f"    card:    https://huggingface.co/datasets/{spec.repo_id}")
        typer.echo(f"    note:    {spec.note}")
        if license_text.startswith("NOT DECLARED"):
            undeclared.append(spec.repo_id)

    if undeclared:
        typer.secho(
            f"\n  WARNING: no licence is declared for {', '.join(undeclared)}. "
            "Absence of a licence is not permission. Use locally for evaluation only, "
            "and do not redistribute.",
            fg=typer.colors.YELLOW,
        )

    if not accept_licenses:
        typer.secho(
            "\nNothing downloaded. Re-run with --accept-licenses once you have read the "
            "cards above.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    typer.secho("\nDownloading", bold=True)
    total = 0
    for spec in specs:
        total += pull(spec, limit if limit is not None else spec.default_limit)

    typer.secho(f"\n{total} images written under data/synthetic/.", fg=typer.colors.GREEN)
    typer.echo("Next: uv run python scripts/index_invoices.py")


def _run() -> int:
    """Run the CLI and return its exit code instead of raising SystemExit."""
    try:
        app()
    except SystemExit as signal:  # typer.Exit surfaces as SystemExit
        return int(signal.code or 0)
    return 0


if __name__ == "__main__":
    code = _run()
    sys.stdout.flush()
    sys.stderr.flush()
    # `datasets` streaming registers a parquet reader for cleanup at interpreter
    # shutdown, and on this stack that teardown does not complete: the process
    # sits there indefinitely after the last image is already on disk. Every byte
    # this script produces is written with Path.write_bytes and the streams are
    # flushed above, so skipping atexit costs nothing and turns a hang into an
    # exit. Confirmed against datasets 5.0.1 / fsspec on macOS arm64.
    os._exit(code)
