"""Decide whether an invoice may already have been received.

Caller: code. The orchestrator calls this; no model can.
Side effects: NONE. Pure: the loop fetches the candidates from ``dedup_index``
and hands them in.

Duplicate payment is the most common way an AP function loses money without
anyone doing anything wrong, and it is a pure data-matching problem - which is
why no model is anywhere near it.

**The rule is the architecture report's**, with its numbers in
``guardrails.v1.yaml`` under ``duplicate_detection``:

* ``near_number`` - same vendor AND normalised invoice number at least
  ``number_similarity_min`` similar AND amount within ``amount_tolerance_pct``
  AND date within ``date_window_days``. The resent-with-a-suffix case:
  ``INV-001`` then ``INV-001A`` for the same goods three days later.
* ``same_amount`` - same vendor AND the same amount within
  ``same_amount_window_days``. The renumbered-resend case.

**Plus one the database forces**, checked first: ``exact_number`` - same vendor
and the same normalised number, whatever the amount or date. ``invoices``
refuses a second ``(vendor_id, invoice_number)``, so without this the second
invoice would fail at the database instead of reaching a person.

**What it never does:** match the invoice against itself (its own row is
excluded by id, so a step restarted after its row was written still sees only
the others), compare amounts across currencies, or decide anything. A match is
a hold for a person; the person decides.
"""

from __future__ import annotations

from decimal import Decimal
from difflib import SequenceMatcher

from pydantic import Field

from ap_agent.contracts.dedup import DedupEntry, DuplicateRule
from ap_agent.contracts.guardrails import DuplicateDetection
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.NONE,)
REQUIRES_IDEMPOTENCY_KEY = False

_HUNDRED = Decimal(100)


class FindDuplicatesInput(ToolInput):
    """The invoice, the earlier ones from the same vendor, and the rule's numbers."""

    entry: DedupEntry
    candidates: list[DedupEntry] = Field(
        max_length=1000,
        description="Earlier invoices from the same vendor near enough in date to matter. "
        "May include the invoice itself; it is never matched.",
    )
    rules: DuplicateDetection


class FindDuplicatesOutput(ToolOutput):
    """Which rule fired, against which earlier invoice - or that none did."""

    rule: DuplicateRule | None = None
    candidate_invoice_id: str | None = Field(default=None, max_length=26)
    similarity: float | None = Field(
        default=None, ge=0, le=1, description="Number similarity of the matched pair."
    )
    candidates_compared: int = Field(ge=0)


def similarity(left: str, right: str) -> float:
    """How alike two normalised invoice numbers are, 0 to 1. ``INV001``/``INV001A`` is 0.92."""
    return SequenceMatcher(None, left, right).ratio()


def _amounts_close(left: Decimal, right: Decimal, tolerance_pct: Decimal) -> bool:
    """Within ``tolerance_pct`` of the larger amount. Both legs symmetric."""
    larger = max(abs(left), abs(right))
    return abs(left - right) * _HUNDRED <= larger * tolerance_pct


def _match(entry: DedupEntry, other: DedupEntry, rules: DuplicateDetection) -> DuplicateRule | None:
    """The first rule ``other`` trips for ``entry``, in order of certainty, or None."""
    if other.vendor_id != entry.vendor_id:
        return None
    if other.normalised_invoice_number == entry.normalised_invoice_number:
        return DuplicateRule.EXACT_NUMBER
    if other.currency != entry.currency:
        return None
    days = abs((other.invoice_date - entry.invoice_date).days)
    if (
        similarity(entry.normalised_invoice_number, other.normalised_invoice_number)
        >= rules.number_similarity_min
        and _amounts_close(entry.amount, other.amount, rules.amount_tolerance_pct)
        and days <= rules.date_window_days
    ):
        return DuplicateRule.NEAR_NUMBER
    if other.amount == entry.amount and days <= rules.same_amount_window_days:
        return DuplicateRule.SAME_AMOUNT
    return None


_PRECEDENCE: dict[DuplicateRule, int] = {
    DuplicateRule.EXACT_NUMBER: 0,
    DuplicateRule.NEAR_NUMBER: 1,
    DuplicateRule.SAME_AMOUNT: 2,
}


def find_duplicates(payload: FindDuplicatesInput) -> FindDuplicatesOutput:
    """Return the strongest match among the candidates, or no match.

    Strongest is by rule (exact number, then near number, then same amount),
    then by nearest date, then by invoice id - so the answer is the same however
    the candidates arrived.
    """
    entry = payload.entry
    others = [c for c in payload.candidates if c.invoice_id != entry.invoice_id]
    found: list[tuple[int, int, str, DuplicateRule, DedupEntry]] = []
    for other in others:
        rule = _match(entry, other, payload.rules)
        if rule is not None:
            days = abs((other.invoice_date - entry.invoice_date).days)
            found.append((_PRECEDENCE[rule], days, other.invoice_id, rule, other))
    if not found:
        return FindDuplicatesOutput(candidates_compared=len(others))
    _, _, _, rule, other = min(found, key=lambda hit: hit[:3])
    return FindDuplicatesOutput(
        rule=rule,
        candidate_invoice_id=other.invoice_id,
        similarity=round(
            similarity(entry.normalised_invoice_number, other.normalised_invoice_number), 4
        ),
        candidates_compared=len(others),
    )
