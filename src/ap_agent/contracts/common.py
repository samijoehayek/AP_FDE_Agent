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
from typing import TYPE_CHECKING, Annotated, Any, cast

from pydantic import BaseModel, ConfigDict, Field

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
