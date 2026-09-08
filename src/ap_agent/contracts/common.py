"""Shared primitives for every contract: the strict base, money, and currency.

Two decisions in this module are load-bearing for the whole system.

``StrictModel`` sets ``extra="forbid"``. That is not tidiness - it is the
control that stops a model from smuggling an unmodelled field (bank account
number, routing number, a "note to the approver") into a typed object that
downstream code trusts. If a field is not declared here, it does not exist.

Money is ``Decimal`` everywhere. A float subtotal that is off by 1e-15 will
silently fail a three-way match tolerance test, and the failure will look like
a vendor problem rather than a type problem.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

CURRENCY_ALLOWLIST: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY", "SEK", "SGD", "MXN"}
)
"""ISO-4217 codes this pipeline will price.

Deliberately small. An invoice in a currency that is not here is an exception
for a human, not a silent pass-through - an unexpected currency is one of the
cheapest signals that a document is not what it claims to be.
"""

MAX_SNIPPET_CHARS = 200
"""Evidence snippets are bounded so a document cannot inflate model context."""

MAX_SUMMARY_CHARS = 700
"""Exception summaries are bounded so they fit in an approval UI without truncation."""


class StrictModel(BaseModel):
    """Base for every contract in this package.

    ``extra="forbid"`` rejects any field the schema does not declare.
    ``frozen=True`` makes contracts value objects: once a contract has been
    hashed into the audit chain, no later code can alter what was recorded.
    ``protected_namespaces=()`` is required because several contracts carry a
    ``model_id`` field, which would otherwise collide with pydantic's reserved
    ``model_`` prefix.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        protected_namespaces=(),
    )


CurrencyCode = Annotated[
    str,
    Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
        description="ISO-4217 code, validated against CURRENCY_ALLOWLIST by the owning model.",
    ),
]

Money = Annotated[
    Decimal,
    Field(
        max_digits=18,
        decimal_places=2,
        description="A monetary amount in the invoice's currency, to the minor unit.",
    ),
]
"""Totals, subtotals, taxes, line extensions. Two decimal places."""

UnitPrice = Annotated[
    Decimal,
    Field(
        ge=0,
        max_digits=18,
        decimal_places=6,
        description="Per-unit price. Six places because catalogue prices are often sub-cent.",
    ),
]

Quantity = Annotated[
    Decimal,
    Field(
        max_digits=18,
        decimal_places=6,
        description="Quantity in the line's unit of measure. May be negative on a credit line.",
    ),
]

TaxRate = Annotated[
    Decimal,
    Field(
        ge=0,
        le=1,
        max_digits=6,
        decimal_places=5,
        description="Fractional rate, not a percentage: 0.20 is 20%.",
    ),
]

Confidence = Annotated[
    float,
    Field(ge=0.0, le=1.0, description="Calibrated confidence in [0, 1]."),
]

Sha256Hex = Annotated[
    str,
    Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="Lowercase hex SHA-256 digest.",
    ),
]


def is_allowed_currency(code: str) -> bool:
    """Return whether ``code`` is in :data:`CURRENCY_ALLOWLIST`."""
    return code.upper() in CURRENCY_ALLOWLIST
