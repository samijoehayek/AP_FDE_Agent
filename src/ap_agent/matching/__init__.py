"""Deterministic three-way matching: pure functions, no I/O, no model.

The tool wrapper lives in ``ap_agent.tools.compute_match``; the reasoning lives
here. Splitting them keeps the part that decides money importable and testable
without a tool harness, and keeps the tool layer thin enough to read.
"""

from __future__ import annotations

from ap_agent.matching.engine import BILLABLE_STATUSES, compute_match
from ap_agent.matching.pairing import (
    IndexedInvoiceLine,
    LinePair,
    PairedLines,
    normalise_description,
    pair_lines,
)

__all__ = [
    "BILLABLE_STATUSES",
    "IndexedInvoiceLine",
    "LinePair",
    "PairedLines",
    "compute_match",
    "normalise_description",
    "pair_lines",
]
