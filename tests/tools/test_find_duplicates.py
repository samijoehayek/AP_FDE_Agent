"""The duplicate rule, case by case, against the shipped thresholds.

Each case states the earlier invoice and the new one, so the situation is
readable from the test alone. The numbers are the real ones from
``config/guardrails.v1.yaml``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ap_agent.contracts.dedup import DedupEntry, DuplicateRule
from ap_agent.guardrails.config import load_guardrails
from ap_agent.text import normalise_invoice_number
from ap_agent.tools.find_duplicates import (
    FindDuplicatesInput,
    FindDuplicatesOutput,
    find_duplicates,
    similarity,
)

DAY = date(2026, 3, 2)


def _entry(  # noqa: PLR0913 - one keyword per column of the entry
    invoice_id: str,
    number: str,
    amount: str,
    on: date = DAY,
    *,
    vendor: str = "58",
    currency: str = "USD",
) -> DedupEntry:
    return DedupEntry(
        invoice_id=invoice_id,
        vendor_id=vendor,
        normalised_invoice_number=normalise_invoice_number(number),
        amount=Decimal(amount),
        currency=currency,
        invoice_date=on,
    )


def _check(new: DedupEntry, *earlier: DedupEntry) -> FindDuplicatesOutput:
    return find_duplicates(
        FindDuplicatesInput(
            entry=new, candidates=list(earlier), rules=load_guardrails().duplicate_detection
        )
    )


def test_the_same_number_from_the_same_vendor_is_held() -> None:
    """The database refuses a second one anyway; a person must say which is real."""
    out = _check(_entry("new", "INV-001", "500.00"), _entry("old", "INV-001", "500.00"))
    assert (out.rule, out.candidate_invoice_id) == (DuplicateRule.EXACT_NUMBER, "old")


def test_the_same_number_printed_differently_is_the_same_number() -> None:
    out = _check(_entry("new", "inv 001", "999.00"), _entry("old", "INV-001", "500.00"))
    assert out.rule is DuplicateRule.EXACT_NUMBER


def test_resent_with_a_suffix_three_days_later_is_held() -> None:
    """INV-001 then INV-001A, same amount, three days apart - the report's case."""
    out = _check(
        _entry("new", "INV-001A", "1500.00", DAY + timedelta(days=3)),
        _entry("old", "INV-001", "1500.00"),
    )
    assert out.rule is DuplicateRule.NEAR_NUMBER
    assert out.candidate_invoice_id == "old"
    assert out.similarity == pytest.approx(0.9231, abs=1e-4)


def test_near_number_allows_half_a_percent_and_no_more() -> None:
    old = _entry("old", "INV-001", "1000.00")
    within = _check(_entry("new", "INV-001A", "1005.00", DAY + timedelta(days=3)), old)
    beyond = _check(_entry("new", "INV-001A", "1005.10", DAY + timedelta(days=3)), old)
    assert within.rule is DuplicateRule.NEAR_NUMBER
    assert beyond.rule is None


def test_near_number_allows_seven_days_and_no_more() -> None:
    old = _entry("old", "INV-001", "1000.00")
    assert _check(_entry("new", "INV-001A", "1000.10", DAY + timedelta(days=7)), old).rule
    assert _check(_entry("new", "INV-001A", "1000.10", DAY + timedelta(days=8)), old).rule is None


def test_the_same_amount_within_thirty_days_is_held() -> None:
    """A renumbered resend: nothing alike in the number, the bill is identical."""
    out = _check(
        _entry("new", "77-4410", "2310.55", DAY + timedelta(days=30)),
        _entry("old", "INV-001", "2310.55"),
    )
    assert out.rule is DuplicateRule.SAME_AMOUNT


def test_two_invoices_a_month_apart_are_not_duplicates() -> None:
    """A monthly bill: same vendor, same amount, different number, 31 days on."""
    out = _check(
        _entry("new", "INV-014", "2310.55", DAY + timedelta(days=31)),
        _entry("old", "INV-001", "2310.55"),
    )
    assert out.rule is None
    assert out.candidates_compared == 1


def test_another_vendor_is_never_a_duplicate() -> None:
    out = _check(
        _entry("new", "INV-001", "500.00"), _entry("old", "INV-001", "500.00", vendor="59")
    )
    assert out.rule is None


def test_amounts_in_different_currencies_are_not_compared() -> None:
    out = _check(
        _entry("new", "INV-001A", "500.00", currency="EUR"), _entry("old", "INV-001", "500.00")
    )
    assert out.rule is None


def test_an_invoice_is_never_its_own_duplicate() -> None:
    """Its own row may be in the candidates - a restarted step - and must not match."""
    me = _entry("me", "INV-001", "500.00")
    out = _check(me, me)
    assert out.rule is None
    assert out.candidates_compared == 0


def test_the_strongest_rule_wins_whatever_the_order() -> None:
    new = _entry("new", "INV-001", "500.00", DAY + timedelta(days=2))
    same_amount = _entry("a", "ZZZ-9", "500.00", DAY + timedelta(days=1))
    exact = _entry("b", "INV-001", "800.00", DAY - timedelta(days=20))
    assert _check(new, same_amount, exact).candidate_invoice_id == "b"
    assert _check(new, exact, same_amount).candidate_invoice_id == "b"


def test_similarity_is_the_difflib_ratio() -> None:
    assert similarity("INV001", "INV001A") == pytest.approx(12 / 13)
    assert similarity("INV001", "INV001") == 1.0
