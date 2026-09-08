"""Golden-set loader and eval fixtures. STUB.

The golden set is the only thing that makes a change to an extraction prompt
reviewable. Without it, "the new prompt is better" is an opinion.

Shape it will take:

* ``data/golden/<case_id>.json`` holds a hand-checked ``InvoiceExtraction`` next
  to the document's sha256. The documents themselves stay under ``data/`` and
  out of git; the labels are small enough to commit once they are synthetic.
* Field-level scoring, not document-level. "94% accurate" hides that the 6% is
  always the total. Money fields are scored exactly; dates exactly; names
  fuzzily; line items by set overlap.
* Extraction is scored against labels, and the matching rules are scored against
  expected ``MatchResult`` reason codes - a wrong reason code on a correct hold
  is still a defect, because it routes to the wrong person.
* Regression gate in CI once the set is large enough to be meaningful.

deepeval is a dev dependency for the LLM-judged parts (exception summaries,
where there is no single right string). Deterministic fields are not judged by a
model - they are compared.

Not implemented: the loader, the scorers, and the fixtures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from ap_agent.contracts.invoice import InvoiceExtraction


class GoldenCase:
    """One labelled document. STUB.

    Will carry: the case id, the document path and sha256, the hand-checked
    expected extraction, and the expected match reason codes.
    """

    def __init__(self) -> None:
        raise NotImplementedError


def load_golden_set(root: Path) -> Sequence[GoldenCase]:
    """Load every labelled case under ``root``.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError


def score_extraction(
    predicted: InvoiceExtraction,
    expected: InvoiceExtraction,
) -> dict[str, float]:
    """Score a predicted extraction field by field.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
