"""Loader for the versioned tolerance and approval config. STUB.

The rules that decide money live in ``config/guardrails.v<N>.yaml``, not in
Python, for three reasons a reviewer should be able to check:

1. **They change on a different clock than the code.** A controller raising the
   price tolerance from 2% to 3% should not require a deploy.
2. **They must be versionable.** Every ``MatchResult`` and every ``AuditEvent``
   carries the ``config_version`` that produced it, so a decision made under
   last quarter's rules can be re-explained under last quarter's rules. That is
   only possible if old versions are files that still exist.
3. **They must be reviewable by someone who does not read Python.** The people
   who own an approval matrix are in finance.

The file is data. It is loaded with ``yaml.safe_load``, validated against the
models here, and rejected if it does not conform - a malformed tolerance is a
startup failure, never a default.

Not implemented: the loader, the validation, the version resolution, and the
approval-matrix lookup. The models below describe the shape the YAML must take
and are the specification the loader will validate against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money, StrictModel

if TYPE_CHECKING:
    from pathlib import Path


class ToleranceBand(StrictModel):
    """One tolerance: a relative band, an absolute floor, or both.

    Both are supplied because neither works alone. A 2% tolerance on a $12 line
    is noise; a $5 tolerance on a $200,000 line is a rounding error. The rule
    passes when the variance is inside *either*.
    """

    relative: float | None = Field(default=None, ge=0.0, le=1.0)
    absolute: Money | None = Field(default=None, ge=0)


class ApprovalRule(StrictModel):
    """One row of the approval matrix."""

    max_amount: Money | None = Field(
        default=None, description="Upper bound of this band. None means unbounded."
    )
    approver_role: str = Field(min_length=1, max_length=64)
    requires_second_approver: bool = False
    applies_to_reason_codes: list[str] = Field(
        default_factory=list[str],
        description="Empty means the band applies regardless of why approval was needed.",
    )


class GuardrailsConfig(StrictModel):
    """The whole ruleset, as loaded from one versioned YAML file."""

    version: str = Field(min_length=1, max_length=32)
    base_currency: CurrencyCode

    price_tolerance: ToleranceBand
    quantity_tolerance: ToleranceBand
    totals_tolerance: ToleranceBand
    tax_tolerance: ToleranceBand

    min_extraction_confidence: float = Field(ge=0.0, le=1.0)
    min_page_sharpness: float = Field(ge=0.0)
    duplicate_score_threshold: float = Field(ge=0.0, le=1.0)
    duplicate_date_window_days: int = Field(ge=0)

    auto_approve_max_amount: Money = Field(
        ge=0,
        description="Above this, a human approves. Setting it to 0 disables straight-through "
        "processing entirely, which is the correct value on day one.",
    )
    approval_matrix: list[ApprovalRule] = Field(default_factory=list[ApprovalRule])

    new_vendor_requires_external_verification: bool = True
    block_on_bank_details_mismatch: bool = True


def load_guardrails(path: Path) -> GuardrailsConfig:
    """Load and validate a versioned guardrails file.

    Args:
        path: Path to a ``guardrails.v<N>.yaml``.

    Returns:
        The validated config.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
