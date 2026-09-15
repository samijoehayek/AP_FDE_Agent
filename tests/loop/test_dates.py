"""Settling an ambiguous invoice date. Pure functions, no clock, no network.

Every case here hands the function its own "today". That is the property under
test as much as the arithmetic is: a rule that read the wall clock would resolve
a date one way today and another way next year, and the audit chain that hashed
the first answer would then look tampered with.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ap_agent.loop.dates import (
    resolve_date_by_locale,
    resolve_date_by_receipt_window,
)

A_YEAR = 365
"""The window these cases were written against.

Passed explicitly now: the value moved into ``config/guardrails.v1.yaml``,
because a window a pure function chose for itself would be a policy decision
that no audit row's ``config_version`` could account for.
"""

MARCH = date(2024, 3, 9)
SEPTEMBER = date(2024, 9, 3)
BOTH = [MARCH, SEPTEMBER]
"""The two readings of ``09/03/2024``, which is invoice 51109305's date."""


# --- the receipt window ------------------------------------------------------


def test_a_candidate_after_the_document_arrived_is_impossible() -> None:
    """An invoice cannot be issued after it was received. That is the whole rule."""
    assert resolve_date_by_receipt_window(BOTH, date(2024, 3, 12), A_YEAR) == MARCH


def test_both_candidates_can_survive() -> None:
    """By October, 9 March and 3 September are both plausible. The rule declines.

    This is the case a six-month window got wrong: it dropped March as too old
    and resolved to September, which is a date the document may well not carry.
    """
    assert resolve_date_by_receipt_window(BOTH, date(2024, 10, 1), A_YEAR) is None


def test_neither_candidate_can_survive() -> None:
    """Received before either reading. Something else is wrong; not this rule's call."""
    assert resolve_date_by_receipt_window(BOTH, date(2024, 1, 1), A_YEAR) is None


def test_an_unambiguous_date_passes_through() -> None:
    assert resolve_date_by_receipt_window([MARCH], date(2024, 3, 12), A_YEAR) == MARCH


def test_no_candidates_resolves_to_nothing() -> None:
    assert resolve_date_by_receipt_window([], date(2024, 3, 12), A_YEAR) is None


def test_a_candidate_older_than_the_window_is_dropped() -> None:
    """A supplier does not invoice for work from three years ago."""
    stale = date(2021, 3, 9)
    assert resolve_date_by_receipt_window([stale, MARCH], date(2024, 3, 12), A_YEAR) == MARCH


def test_the_window_is_a_parameter_not_a_constant() -> None:
    """It belongs in the guardrails config once that exists; the default is a guess."""
    recent = date(2024, 3, 1)
    older = date(2023, 12, 1)
    received = date(2024, 3, 12)
    assert resolve_date_by_receipt_window([older, recent], received, max_age_days=30) == recent
    assert resolve_date_by_receipt_window([older, recent], received, max_age_days=400) is None


def test_a_candidate_exactly_on_the_boundary_survives() -> None:
    """Inclusive at both ends: an invoice issued the day it arrived is ordinary."""
    received = date(2024, 3, 12)
    oldest = received - timedelta(days=A_YEAR)
    assert resolve_date_by_receipt_window([received], received, A_YEAR) == received
    assert resolve_date_by_receipt_window([oldest], received, A_YEAR) == oldest
    assert resolve_date_by_receipt_window([oldest - timedelta(days=1)], received, A_YEAR) is None


@pytest.mark.parametrize("received", [date(2024, 3, 12), date(2025, 1, 1), date(2024, 9, 4)])
def test_the_answer_depends_only_on_the_arguments(received: date) -> None:
    """Called twice with the same inputs, twice the same answer. Replay depends on it."""
    first = resolve_date_by_receipt_window(BOTH, received, A_YEAR)
    assert first == resolve_date_by_receipt_window(BOTH, received, A_YEAR)


# --- the vendor's locale -----------------------------------------------------


def test_an_indian_vendor_reads_day_first() -> None:
    assert resolve_date_by_locale("09/03/2024", "IN") == MARCH


def test_a_us_vendor_reads_month_first() -> None:
    assert resolve_date_by_locale("09/03/2024", "US") == SEPTEMBER


def test_an_unknown_country_settles_nothing() -> None:
    """Leaves the date open rather than falling back to the more common convention."""
    assert resolve_date_by_locale("09/03/2024", None) is None
    assert resolve_date_by_locale("09/03/2024", "ZZ") is None


def test_no_rendering_settles_nothing() -> None:
    """Without the digits there is nothing to resolve.

    The candidates alone cannot say which of them was the day-first reading.
    """
    assert resolve_date_by_locale(None, "IN") is None
    assert resolve_date_by_locale("", "IN") is None


def test_a_rendering_that_is_not_a_date_settles_nothing() -> None:
    assert resolve_date_by_locale("not a date", "IN") is None
