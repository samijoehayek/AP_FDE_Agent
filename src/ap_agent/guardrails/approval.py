"""APPROVAL-MATRIX@v1: who must approve an invoice, read from the guardrails file.

Responsible for one answer - how many people, of which role - and the working
behind it, as a basis an auditor can re-check against ``guardrails.v1.yaml``.

How it decides:

* **The tier** is chosen by amount, smallest ceiling first. An invoice in a
  currency other than ``base_currency`` gets the **top tier**: there is no
  exchange rate in this system, and comparing 272,100 INR to a dollar ceiling
  would be a number that looks like an answer. The conservative tier is the
  honest one until an FX source exists.
* **The rules** each name a condition and a count. A rule that
  ``overrides_tiers`` raises the requirement to its count whatever the amount;
  one that does not can never lower it. Either way the larger count wins.
  ``hold_for_callback`` from any rule that fires is carried on the answer.
* **Straight-through approval is not built.** ``auto_approve_max_amount`` is
  0.00, so every invoice goes to a person - and if the file is ever changed to
  allow some, this rule still routes them to a person and says so, rather than
  approving on a path nobody has tested.

Pure: no I/O, no clock. Whether this is the vendor's first invoice is a fact
the caller looks up and passes in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ap_agent.contracts.approval import ApprovalRequirement

if TYPE_CHECKING:
    from decimal import Decimal

    from ap_agent.contracts.guardrails import ApprovalMatrix

__all__ = ["RULE_ID", "approval_requirement"]

RULE_ID = "APPROVAL-MATRIX@v1"


def approval_requirement(  # noqa: PLR0913 - one keyword per fact the matrix keys on
    matrix: ApprovalMatrix,
    *,
    base_currency: str,
    config_version: str,
    amount: Decimal,
    currency: str,
    vendor_resolved: bool,
    first_invoice_from_vendor: bool,
    remit_to_mismatch: bool,
    match_exception: bool,
) -> ApprovalRequirement:
    """Apply the matrix to one invoice's facts. See the module docstring."""
    foreign = currency != base_currency
    tiers = matrix.tiers
    tier = (
        tiers[-1]
        if foreign
        else next((t for t in tiers if t.max_amount is None or amount <= t.max_amount), tiers[-1])
    )

    conditions = {
        "new_vendor": not vendor_resolved,
        "first_invoice_from_vendor": vendor_resolved and first_invoice_from_vendor,
        "remit_to_mismatch": remit_to_mismatch,
        "match_exception": match_exception,
    }
    fired = [rule for rule in matrix.rules if conditions.get(rule.name, False)]
    required = max([tier.approvers_required, *(rule.approvers_required for rule in fired)])
    callback = any(rule.hold_for_callback for rule in fired)

    within_auto = matrix.auto_approve_max_amount > 0 and amount <= matrix.auto_approve_max_amount
    auto = (
        "off (auto_approve_max_amount=0.00)"
        if matrix.auto_approve_max_amount == 0
        else "within limit, but straight-through approval is not built: routed to a person"
        if within_auto
        else f"over auto_approve_max_amount={matrix.auto_approve_max_amount}"
    )
    ceiling = "unbounded" if tier.max_amount is None else str(tier.max_amount)
    basis = ", ".join(
        [
            f"rule={RULE_ID}",
            f"config_version={config_version}",
            f"amount={amount} {currency}",
            (
                f"tier=top (currency {currency} != base {base_currency}, no FX rate)"
                if foreign
                else f"tier<= {ceiling}"
            ),
            f"role={tier.approver_role}",
            f"required={required}",
            f"rules={'|'.join(rule.name for rule in fired) or 'none'}",
            f"hold_for_callback={'yes' if callback else 'no'}",
            f"auto_approve={auto}",
        ]
    )
    return ApprovalRequirement(
        required_approvers=required,
        approver_role=tier.approver_role,
        tier_max_amount=tier.max_amount,
        rules_fired=[rule.name for rule in fired],
        hold_for_callback=callback,
        amount=amount,
        currency=currency,
        auto_approve=False,
        basis=basis[:1000],
    )
