"""What the approval matrix requires of one invoice, and why.

Responsible for the shape of ``APPROVAL-MATRIX@v1``'s answer. The rule is in
``guardrails/approval.py``; the durable request it becomes is an ``approvals``
row. No prose from the document is in here - amounts, codes, and the names of
the matrix's own tiers and rules.
"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money, StrictModel

__all__ = ["ApprovalRequirement"]


class ApprovalRequirement(StrictModel):
    """How many people, of what role, must approve - and the rule's working."""

    required_approvers: int = Field(ge=1, le=4)
    approver_role: str = Field(min_length=1, max_length=64)
    tier_max_amount: Money | None = Field(
        description="The ceiling of the tier applied; None is the unbounded top tier."
    )
    rules_fired: list[str] = Field(
        default_factory=list[str], description="Matrix rules that applied, by name."
    )
    hold_for_callback: bool = Field(
        description="A person must phone the vendor on a number already on file first."
    )
    amount: Money
    currency: CurrencyCode
    auto_approve: bool = Field(
        description="Always false in this build: straight-through approval is not built, and "
        "the shipped limit is 0.00 anyway."
    )
    basis: str = Field(min_length=1, max_length=1000, description="The audit row's basis.")
