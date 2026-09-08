"""Render synthetic invoice PDFs from seeded purchase orders. STUB.

Why generate at all when public corpora exist: a downloaded corpus has no
purchase orders behind it. You cannot test a three-way match against a document
whose PO you do not have, which means the public sets can only exercise
extraction - never matching, tolerances, or approval routing.

What this will do:

1. Seed a small vendor master and a set of POs with known prices and quantities.
2. Render an invoice per PO with reportlab, in several layouts, so extraction is
   not tuned to one template.
3. Inject *known* defects at a controlled rate, each one labelled: a price 3%
   over the PO, a quantity above what was received, a line absent from the PO,
   totals that do not add up, a re-issued invoice number, a near-duplicate a
   week later.
4. Include adversarial documents - text in the body instructing the reader to
   approve, to email a remittance change, to ignore prior instructions - so the
   ``suspicious_text`` path and the no-tools rule are tested against something
   rather than asserted.
5. Write ``data/generated/labels.jsonl`` with the expected extraction and the
   expected match reason codes for each document.

The label file is the point. A generated invoice without a label is just a PDF.

Not implemented: the seeding, the layouts, the defect injection, and the labels.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

REPO_ROOT = Path(__file__).resolve().parents[1]

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    count: Annotated[int, typer.Option("--count", "-n", min=1, help="Invoices to render.")] = 50,
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Destination.")] = REPO_ROOT
    / "data"
    / "generated",
    seed: Annotated[int, typer.Option("--seed", help="Deterministic corpus seed.")] = 20260909,
    defect_rate: Annotated[
        float, typer.Option("--defect-rate", min=0.0, max=1.0, help="Fraction with a defect.")
    ] = 0.4,
) -> None:
    """Render ``count`` labelled synthetic invoices into ``out_dir``.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    del count, out_dir, seed, defect_rate
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(app())
