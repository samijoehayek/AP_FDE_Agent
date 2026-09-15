"""The rules that decide money, as a typed object loaded from versioned YAML.

This is the config ``compute_match`` takes as an argument and stamps on every
``MatchResult`` it produces. It is a contract rather than a dict for the reason
every other contract here is: the shape is checked once, at the boundary, and
everything downstream reads a typed object instead of guessing at keys.

Three properties are deliberate.

**Nothing has a default.** Every field is required, so a value missing from the
file is a loud startup failure rather than a quietly different answer. A
tolerance that falls back to a constant when the file is incomplete is a
tolerance nobody can audit - the trail would name a config version whose numbers
were partly invented.

**``extra="forbid"`` everywhere.** A key the schema does not declare fails
loading. That catches the more dangerous half of a typo: ``price_variance_pcnt``
would otherwise leave the real field at whatever it was and be silently ignored,
which is the failure mode where a tolerance is wider than anyone believes.

**Percentages are percent, not fractions.** ``2.0`` is two percent. The report
this is built from writes them that way, the people who own these numbers write
them that way, and a file that silently meant 200% would be a bad way to find
out that a fraction was expected.

What this module deliberately does not contain is any *evaluation*. Nothing here
compares an invoice to a purchase order. These are the numbers; applying them is
``compute_match``'s job, and keeping the two apart is what lets the numbers be
reviewed by someone who does not read Python.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money, StrictModel

Percentage = Annotated[
    Decimal,
    Field(
        ge=0,
        le=100,
        max_digits=6,
        decimal_places=3,
        allow_inf_nan=False,
        description="A percentage, not a fraction: 2.0 is two percent.",
    ),
]


class FilterAction(StrEnum):
    """What happens when an output-filter pattern matches.

    One member today, and the enum exists to keep it that way. The tempting
    second member is "redact", and it is wrong: stripping an IBAN out of a model
    response destroys the evidence that somebody put one there, which is the
    fact a reviewer most needs.
    """

    ROUTE_TO_HUMAN = "route_to_human"


class Tolerances(StrictModel):
    """How far an invoice may differ from its purchase order before a person looks."""

    price_variance_pct: Percentage = Field(
        description="Unit price band. Holds together with price_variance_abs - both must "
        "pass, so the tighter one binds."
    )
    price_variance_abs: Money = Field(
        ge=0, description="Absolute per-line price band, in base_currency."
    )
    qty_over_billing_pct: Percentage = Field(
        description="How far above the received quantity an invoice may bill. Zero: "
        "over-billing is a claim about goods that do not exist, not a measurement error."
    )
    qty_under_billing_pct: Percentage = Field(
        description="How far below the received quantity an invoice may bill. A shortfall "
        "is the vendor's loss and nobody's exception."
    )
    unmatched_charge_abs: Money = Field(
        ge=0,
        description="Freight, handling, anything no purchase-order line covers. Under this "
        "it rides along; over it, a human looks.",
    )
    invoice_max_age_days: int = Field(
        ge=1,
        description="How far before its arrival an invoice may plausibly have been issued. "
        "Read by the date-resolution rule and by the staleness check, which is why it "
        "lives here rather than as a constant in the loop.",
    )
    tax_variance_abs: Money = Field(
        ge=0,
        description="Absolute only: a percentage band on tax is a percentage of a "
        "percentage and stops meaning anything.",
    )
    rounding_tolerance_abs: Money = Field(
        ge=0,
        description="What is left after every line-level check has passed. Rounding, and "
        "nothing else - anything larger should already have been caught on a line.",
    )


class ApprovalTier(StrictModel):
    """One amount band of the approval matrix."""

    max_amount: Money | None = Field(
        default=None,
        ge=0,
        description="Upper bound of this band, inclusive. None is the unbounded top band.",
    )
    approver_role: str = Field(min_length=1, max_length=64)
    approvers_required: int = Field(ge=1, le=4)


class ApprovalRule(StrictModel):
    """A condition that decides approval regardless of the amount."""

    name: str = Field(min_length=1, max_length=64)
    when: str = Field(
        min_length=1,
        max_length=300,
        description="The condition in prose, for the person reviewing this file. Code keys "
        "off `name`; this is what makes the row reviewable by someone in finance.",
    )
    approvers_required: int = Field(ge=1, le=4)
    overrides_tiers: bool = Field(
        description="True when the amount band may not lower the requirement. A $40 invoice "
        "from a vendor nobody has onboarded still needs two people."
    )
    hold_for_callback: bool = Field(
        description="True when approval waits on out-of-band verification - a phone call to "
        "a number already on file, never one printed on the document."
    )


class ApprovalMatrix(StrictModel):
    """Who signs off, at what amount, and what overrides the amount."""

    auto_approve_max_amount: Money = Field(
        ge=0,
        description="Below this a clean invoice needs no human. Zero disables "
        "straight-through processing entirely, which is the correct day-one value: "
        "turning it on should be a reviewed change with a golden-set number attached.",
    )
    tiers: list[ApprovalTier] = Field(min_length=1, max_length=10)
    rules: list[ApprovalRule] = Field(min_length=1, max_length=20)


class InputValidation(StrictModel):
    """Limits applied before any model call.

    Before, so that a hostile file is refused without ever being read by
    something that could act on it.
    """

    max_pages: int = Field(
        ge=1, description="A 20-page invoice is a statement, a contract, or an attack."
    )
    max_file_bytes: int = Field(ge=1)
    allowed_mime_types: list[str] = Field(min_length=1, max_length=20)
    max_line_items: int = Field(ge=1)
    max_field_length: int = Field(ge=1)


class OutputFilterPattern(StrictModel):
    """One thing a model's output must not contain."""

    name: str = Field(min_length=1, max_length=64)
    pattern: str = Field(
        min_length=1, max_length=500, description="A regular expression, applied to string fields."
    )
    why: str = Field(
        min_length=1,
        max_length=300,
        description="Why this pattern is here. A filter nobody can explain is a filter "
        "somebody eventually deletes.",
    )


class OutputFilter(StrictModel):
    """What model output may not carry into code that acts on it."""

    action: FilterAction
    never_auto_fix: bool = Field(
        description="Always true, and stated rather than assumed. Silently stripping an IBAN "
        "out of a field would destroy the evidence that somebody tried to put one there."
    )
    patterns: list[OutputFilterPattern] = Field(min_length=1, max_length=50)


class HardProhibitions(StrictModel):
    """Tools that must not exist. Not thresholds - absences.

    The authority for this is ``FORBIDDEN_TOOL_NAMES`` in ``ap_agent.tools``, in
    code, and this section is asserted to agree with it. The direction matters:
    a prohibition that could be lifted by editing a YAML file is not a
    prohibition, so the file documents the rule and the code enforces it.
    """

    forbidden_tool_names: list[str] = Field(min_length=1, max_length=50)
    forbidden_tool_prefixes: list[str] = Field(min_length=1, max_length=50)


class GuardrailConfig(StrictModel):
    """One versioned ruleset, as loaded from ``config/guardrails.v<N>.yaml``.

    ``config_version`` is what a ``MatchResult`` and an ``AuditEvent`` record, so
    a decision can be re-explained later under the rules that actually made it.
    The loader checks it against the filename, because a file whose contents
    disagree with its own name makes that lookup a guess.
    """

    config_version: str = Field(
        min_length=1,
        max_length=32,
        description="Stamped on every MatchResult. Must match the file it was loaded from.",
    )
    base_currency: CurrencyCode

    tolerances: Tolerances
    approval_matrix: ApprovalMatrix
    input_validation: InputValidation
    output_filter: OutputFilter
    hard_prohibitions: HardProhibitions


__all__ = [
    "ApprovalMatrix",
    "ApprovalRule",
    "ApprovalTier",
    "FilterAction",
    "GuardrailConfig",
    "HardProhibitions",
    "InputValidation",
    "OutputFilter",
    "OutputFilterPattern",
    "Percentage",
    "Tolerances",
]
