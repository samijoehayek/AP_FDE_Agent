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

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

CURRENCY_ALLOWLIST: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "INR", "CAD", "AUD", "CHF", "JPY", "SEK", "SGD", "MXN"}
)
"""ISO-4217 codes this pipeline will price.

Deliberately small. An invoice in a currency that is not here is an exception
for a human, not a silent pass-through - an unexpected currency is one of the
cheapest signals that a document is not what it claims to be.

The list is a calibration, not a constant: it should hold the currencies the
business actually trades in and nothing more. INR is here because every invoice
in the Kaggle corpus is Indian, which the first live extraction discovered by
being refused. Widen it when the corpus or the business does - never to make a
single awkward document go through.
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

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Drop pydantic's auto-generated ``title`` keys from the JSON schema.

        Structured outputs enforces an undocumented complexity budget, and
        ``InvoiceExtraction`` runs close to it - close enough that adding three
        string fields once tipped it into ``400 Schema is too complex``.

        Titles are the cheapest thing to give up because they carry no
        information at all: pydantic derives ``"title": "Vendor Address"`` from
        the key ``vendor_address``, which the model can already read. Measured on
        the extraction schema, they cost 822 bytes - more than the headroom the
        three fields needed.

        Descriptions are kept. Those are the field-level instructions the model
        actually extracts against, and trading them for schema budget would buy
        space with accuracy.
        """
        schema = handler(core_schema)
        schema.pop("title", None)
        properties: dict[str, Any] = schema.get("properties", {})
        for subschema in properties.values():
            if isinstance(subschema, dict):
                cast("dict[str, Any]", subschema).pop("title", None)
        return schema


CurrencyCode = Annotated[
    str,
    Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
        description="ISO-4217 code, validated against CURRENCY_ALLOWLIST by the owning model.",
    ),
]

MAX_MONEY_DIGITS: Final = 18
MONEY_PLACES: Final = 2
COST_PLACES: Final = 6
"""API costs are fractions of a cent, so two places would round them to nothing."""

PRICE_PLACES: Final = 6
"""Unit prices and quantities. Catalogue prices are often sub-cent."""

RATE_PLACES: Final = 5
"""Tax rates as fractions: 0.20 is 20%."""

_MONEY_EXPONENT: Final = Decimal("0.01")
_COST_EXPONENT: Final = Decimal("0.000001")
_PRICE_EXPONENT: Final = Decimal("0.000001")
_RATE_EXPONENT: Final = Decimal("0.00001")


def _canonicalise(value: Decimal, exponent: Decimal, places: int) -> Decimal:
    """Return ``value`` at exactly the scale of ``exponent``, never rounding.

    Canonical form matters beyond tidiness. The audit trail hashes a
    serialisation of each event, so ``Decimal("1676976")`` and
    ``Decimal("1676976.00")`` - equal numbers, different strings - would produce
    different hashes for the same extraction. A chain that reports tampering
    when nothing was tampered with is a chain people learn to ignore.

    Rounding is refused rather than performed. The field constraints already
    reject too many decimal places, so quantising here is exact by construction;
    if that ever stops being true, the right outcome is a loud failure, not a
    silently altered amount.
    """
    if not value.is_finite():
        msg = f"not a finite amount: {value}"
        raise ValueError(msg)

    try:
        quantised = value.quantize(exponent)
    except InvalidOperation as exc:
        msg = f"amount cannot be represented at {places} places: {value}"
        raise ValueError(msg) from exc

    if quantised != value:
        msg = f"amount would need rounding to be stored: {value}"
        raise ValueError(msg)

    if len(quantised.as_tuple().digits) > MAX_MONEY_DIGITS:
        msg = f"amount exceeds {MAX_MONEY_DIGITS} significant digits once scaled: {value}"
        raise ValueError(msg)

    # Decimal("-0.00") is equal to zero but serialises with a sign, which would
    # break the hash the same way a trailing zero would.
    return Decimal(0).quantize(exponent) if quantised.is_zero() else quantised


def _canonical_money(value: Decimal) -> Decimal:
    return _canonicalise(value, _MONEY_EXPONENT, MONEY_PLACES)


def _canonical_cost(value: Decimal) -> Decimal:
    return _canonicalise(value, _COST_EXPONENT, COST_PLACES)


def _canonical_price(value: Decimal) -> Decimal:
    return _canonicalise(value, _PRICE_EXPONENT, PRICE_PLACES)


def _canonical_rate(value: Decimal) -> Decimal:
    return _canonicalise(value, _RATE_EXPONENT, RATE_PLACES)


Money = Annotated[
    Decimal,
    Field(
        max_digits=MAX_MONEY_DIGITS,
        decimal_places=MONEY_PLACES,
        allow_inf_nan=False,
        description="A monetary amount in the invoice's currency, to the minor unit.",
    ),
    AfterValidator(_canonical_money),
]
"""Totals, subtotals, taxes, line extensions.

Always exactly two decimal places once validated, whatever the document printed,
so that equal amounts serialise identically.
"""

CostUsd = Annotated[
    Decimal,
    Field(
        max_digits=MAX_MONEY_DIGITS,
        decimal_places=COST_PLACES,
        allow_inf_nan=False,
        description="A cost in USD, to six places.",
    ),
    AfterValidator(_canonical_cost),
]
"""What an API call cost. Not :data:`Money`.

A single extraction costs about $0.027, which two decimal places would record as
$0.03 - or, as the contract was originally written, reject outright. Six places
matches the ``audit_events.cost_usd`` column.
"""

UnitPrice = Annotated[
    Decimal,
    Field(
        ge=0,
        max_digits=MAX_MONEY_DIGITS,
        decimal_places=PRICE_PLACES,
        allow_inf_nan=False,
        description="Per-unit price. Six places because catalogue prices are often sub-cent.",
    ),
    AfterValidator(_canonical_price),
]

Quantity = Annotated[
    Decimal,
    Field(
        max_digits=MAX_MONEY_DIGITS,
        decimal_places=PRICE_PLACES,
        allow_inf_nan=False,
        description="Quantity in the line's unit of measure. May be negative on a credit line.",
    ),
    AfterValidator(_canonical_price),
]

TaxRate = Annotated[
    Decimal,
    Field(
        ge=0,
        le=1,
        max_digits=6,
        decimal_places=RATE_PLACES,
        allow_inf_nan=False,
        description="Fractional rate, not a percentage: 0.20 is 20%.",
    ),
    AfterValidator(_canonical_rate),
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
