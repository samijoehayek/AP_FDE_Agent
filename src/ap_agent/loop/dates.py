"""Settling an ambiguous invoice date from facts the loop has and the page does not.

``09/03/2024`` is 9 March or 3 September and the document says nothing more. The
confidence check refuses to guess and carries both readings forward; this module
holds the rules that can close the question, in the order the pipeline learns
enough to apply them.

The receipt window is the first and needs nothing external. A document cannot be
issued after it arrived, and a supplier does not invoice for work half a decade
old, so one candidate is often simply impossible. On the Kaggle corpus that
settles most ambiguous dates before the vendor is even resolved.

Everything here is a pure function and **takes the clock as an argument**. That
is the same rule that keeps ``datetime.now()`` out of the contracts: a date this
resolves must resolve the same way when the run is replayed next year, and a
function that reads the wall clock would quietly produce a different answer and
break the audit chain that hashes it.

This module decides nothing about *whether* an invoice proceeds. It answers
"which of these two dates is it", and hands the answer back to the loop.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from ap_agent.tools.compute_extraction_confidence import (
    country_reads_day_first,
    parse_raw_date,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

MAX_INVOICE_AGE_DAYS = 365
"""How far before its arrival an invoice may plausibly have been issued.

A year, and deliberately generous. A narrower window resolves more dates, which
is tempting - but the two failure modes are not symmetric. Too wide and the rule
declines to choose, the date stays open, and the vendor's locale settles it two
states later. Too narrow and the rule *silently picks the wrong date* by
eliminating a candidate that was in fact correct, and nothing downstream can
tell. Given that asymmetry the window should sit at the edge of plausibility,
not at the middle of it.

Six months was the first value here and it was wrong for exactly this reason: it
resolved 09/03/2024 to September for anything received after early October,
which is a real date on a real invoice and not a candidate to eliminate.

A starting value either way. It belongs in the versioned guardrails config once
that exists, not baked into a module constant.
"""


def resolve_date_by_receipt_window(
    candidates: Sequence[date],
    received_at: date,
    max_age_days: int = MAX_INVOICE_AGE_DAYS,
) -> date | None:
    """Return the only candidate that could have been received on ``received_at``.

    A candidate is impossible if it falls after the document arrived, or more
    than ``max_age_days`` before it. If exactly one survives, that is the date.

    Args:
        candidates: The readings still live. One element is the unambiguous
            case and passes straight through when it is plausible.
        received_at: The day the document arrived, supplied by the caller. Never
            read from a clock inside this function.
        max_age_days: How far back an invoice may plausibly be dated.

    Returns:
        The single surviving candidate, or ``None`` when none or several
        survive. ``None`` means "still open", not "invalid" - the caller carries
        the candidates forward for a later rule to settle.
    """
    earliest = received_at - timedelta(days=max_age_days)
    survivors = [candidate for candidate in candidates if earliest <= candidate <= received_at]
    return survivors[0] if len(survivors) == 1 else None


def resolve_date_by_locale(raw_date_text: str | None, vendor_country: str | None) -> date | None:
    """Return the reading the vendor's country implies, or None if it cannot say.

    The second rule, and the one that needs the vendor master. It re-reads the
    rendering the confidence check carried forward rather than choosing between
    the two candidate dates, because the candidates alone do not record which of
    them was day-first - ``{2024-03-09, 2024-09-03}`` is the same pair whether
    the page said ``09/03`` or ``03/09``.

    Args:
        raw_date_text: The digits as the page renders them.
        vendor_country: ISO-3166-1 alpha-2, from the vendor master.

    Returns:
        The resolved date, or ``None`` when either input is missing or the
        country is one the locale table does not cover. ``None`` leaves the date
        open rather than guessing.
    """
    if not raw_date_text:
        return None
    day_first = country_reads_day_first(vendor_country)
    if day_first is None:
        return None
    raw = parse_raw_date(raw_date_text)
    return raw.resolve(day_first=day_first) if raw is not None else None


__all__ = [
    "MAX_INVOICE_AGE_DAYS",
    "resolve_date_by_locale",
    "resolve_date_by_receipt_window",
]
