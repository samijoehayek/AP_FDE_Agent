"""APPROVAL-MATRIX@v1 against the shipped matrix, one fact at a time."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ap_agent.contracts.approval import ApprovalRequirement
from ap_agent.guardrails.approval import approval_requirement
from ap_agent.guardrails.config import load_guardrails


def _require(  # noqa: PLR0913 - one keyword per fact the matrix keys on
    amount: str,
    currency: str = "USD",
    *,
    resolved: bool = True,
    first: bool = False,
    remit_mismatch: bool = False,
    exception: bool = False,
) -> ApprovalRequirement:
    config = load_guardrails()
    return approval_requirement(
        config.approval_matrix,
        base_currency=config.base_currency,
        config_version=config.config_version,
        amount=Decimal(amount),
        currency=currency,
        vendor_resolved=resolved,
        first_invoice_from_vendor=first,
        remit_to_mismatch=remit_mismatch,
        match_exception=exception,
    )


@pytest.mark.parametrize(
    ("amount", "role", "required"),
    [
        ("10.00", "ap_clerk", 1),
        ("1000.00", "ap_clerk", 1),
        ("1000.01", "controller", 1),
        ("10000.00", "controller", 1),
        ("10000.01", "owner", 2),
        ("272100.00", "owner", 2),
    ],
)
def test_the_tier_follows_the_amount(amount: str, role: str, required: int) -> None:
    out = _require(amount)
    assert (out.approver_role, out.required_approvers) == (role, required)
    assert out.rules_fired == []


def test_another_currency_gets_the_top_tier() -> None:
    """No exchange rate: 900 INR is not "under $1,000", it is unknown. Conservative."""
    out = _require("900.00", "INR")
    assert (out.approver_role, out.required_approvers) == ("owner", 2)
    assert "tier=top (currency INR != base USD, no FX rate)" in out.basis


def test_a_vendor_s_first_invoice_needs_two_people_whatever_the_amount() -> None:
    out = _require("40.00", first=True)
    assert out.required_approvers == 2
    assert out.approver_role == "ap_clerk"
    assert out.rules_fired == ["first_invoice_from_vendor"]


def test_a_remit_to_mismatch_holds_for_a_callback() -> None:
    out = _require("40.00", remit_mismatch=True)
    assert out.required_approvers == 2
    assert out.hold_for_callback is True


def test_an_accepted_exception_never_lowers_the_tier() -> None:
    """match_exception asks for one approver and does not override: the tier's two stand."""
    out = _require("50000.00", exception=True)
    assert out.required_approvers == 2
    assert out.rules_fired == ["match_exception"]


def test_nothing_is_approved_straight_through() -> None:
    out = _require("0.01")
    assert out.auto_approve is False
    assert "auto_approve=off (auto_approve_max_amount=0.00)" in out.basis


def test_the_basis_names_the_rule_and_the_amount_compared() -> None:
    assert _require("272100.00").basis == (
        "rule=APPROVAL-MATRIX@v1, config_version=guardrails_v1, amount=272100.00 USD, "
        "tier<= unbounded, role=owner, required=2, rules=none, hold_for_callback=no, "
        "auto_approve=off (auto_approve_max_amount=0.00)"
    )
