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

import re
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


# ---------------------------------------------------------------------------
# reading a number off a document
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOLS: Final = "$€£₹¥₩₪₫₦₱฿"
"""Symbols a document may print against an amount. Stripped, never interpreted.

The currency a number is *in* is a separate field with its own allowlist. A
symbol next to the digits is typography.
"""

_GROUPING_SPACES: Final = "\u0020\u00a0\u202f\u2009\t"
"""Space characters invoice typography uses to group digits: ordinary,
non-breaking, narrow no-break, thin, and tab."""

_STRIPPABLE: Final = re.compile(f"[{re.escape(_CURRENCY_SYMBOLS + _GROUPING_SPACES)}]")

_DECIMAL_SHAPED: Final = re.compile(r"^[+-]?\d+(\.\d+)?$")

GROUP_DIGITS: Final = 3
"""Digits in a thousands group."""

LAKH_DIGITS: Final = 2
"""Digits in an Indian lakh group: ``1,67,560`` is one hundred and sixty-seven thousand."""

_BOTH_SEPARATORS: Final = 2
"""Both ``,`` and ``.`` present, which fixes which one is the decimal mark."""


class AmbiguousNumber(ValueError):  # noqa: N818 - it is a ValueError, not an app error
    """A printed number that could be read two ways, and nothing settles which.

    Raised rather than guessed at. ``1,234`` is one thousand two hundred and
    thirty-four to a US reader and one point two three four to a German one, and
    a system that picks silently is wrong by a factor of a thousand on an
    invoice it will then pay. A refusal routes to a person; a guess does not.
    """


def normalise_decimal_text(text: str) -> Decimal:
    """Read a number as a document printed it, or refuse to.

    **The rule, in one sentence: refuse only when the string has more than one
    possible reading.**

    Documents group digits and place the decimal mark by local convention, and a
    model transcribing "what is printed" reports exactly that. This is the one
    place that turns those renderings into a number - the grounding check in
    ``compute_extraction_confidence`` calls it too, because two implementations
    would disagree the first time either was edited, and they would disagree
    about money.

    Everything follows from counting readings:

    * **Both ``,`` and ``.`` appear.** Whichever comes last is the decimal mark
      and the other is grouping; nothing else is possible. ``272,100.00`` and
      ``1.234,56`` are each one reading.
    * **One separator kind, appearing more than once.** There cannot be two
      decimal marks, so every one of them is grouping. ``1,234,567`` and
      ``1.234.567`` are each one reading, and both resolve.
    * **One separator, once.** Now it depends on the tail. A three-digit tail
      could be a thousands group *or* a fraction, so ``1,234`` and ``1.234`` are
      two readings and refuse. Any other tail cannot be a group, so ``1,23``
      (1.23) and ``9.250000`` are one reading and resolve.
    * **Grouping that is not well formed is not a reading at all.** ``1.2.3``
      has no valid group shape and no decimal interpretation left, so it
      refuses as malformed rather than as ambiguous.

    This supersedes an earlier, blunter rule that refused a lone comma outright
    - which sent ``1,23`` to a person even though 1.23 is the only thing it can
    mean. Refusing a string that has exactly one reading buys no safety and
    costs a review.

    The refusals that remain are the ones worth having. ``1,234`` is one
    thousand two hundred and thirty-four to one reader and one point two three
    four to another: a factor of a thousand on an invoice this system would then
    pay, with nothing downstream able to tell. A refusal costs one trip to a
    person.

    Args:
        text: The number as printed.

    Returns:
        The value, with grouping removed and ``.`` as the decimal mark.

    Raises:
        AmbiguousNumber: The rendering has more than one reading, has none, or
            is not a number at all.
    """
    had_grouping_space = any(char in _GROUPING_SPACES for char in text.strip())
    cleaned = _STRIPPABLE.sub("", text).strip()
    if not cleaned:
        msg = f"not a number: {text!r}"
        raise AmbiguousNumber(msg)

    kinds = {char for char in cleaned if char in ",."}

    if len(kinds) == _BOTH_SEPARATORS:
        cleaned = _resolve_both_separators(text, cleaned)
    elif len(kinds) == 1:
        cleaned = _resolve_single_separator(text, cleaned, kinds.pop(), spaced=had_grouping_space)

    if not _DECIMAL_SHAPED.match(cleaned):
        msg = f"not a number: {text!r}"
        raise AmbiguousNumber(msg)
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - shape check already passed
        msg = f"not a number: {text!r}"
        raise AmbiguousNumber(msg) from exc


def is_well_formed_grouping(digits: str, mark: str) -> bool:
    """Whether ``digits`` is a plausible grouped integer under ``mark``.

    Two conventions are accepted, because invoices in this corpus use both:

    * **Western.** Every group after the first is exactly three digits, and the
      first is one to three. ``1,234,567``.
    * **Indian lakh.** The last group is three digits and every group before it
      except the first is two. ``1,67,560``.

    Anything else is not grouping - which does not by itself mean the string is
    wrong, only that this reading of it is unavailable.
    """
    groups = digits.split(mark)
    if len(groups) < _BOTH_SEPARATORS or not all(group.isdigit() for group in groups):
        return False
    first, rest = groups[0], groups[1:]
    if not 1 <= len(first) <= GROUP_DIGITS:
        return False
    if all(len(group) == GROUP_DIGITS for group in rest):
        return True
    # Lakh: the final group is a thousand, everything between is a pair.
    return (
        len(first) <= LAKH_DIGITS
        and len(rest[-1]) == GROUP_DIGITS
        and all(len(group) == LAKH_DIGITS for group in rest[:-1])
    )


def _resolve_both_separators(original: str, cleaned: str) -> str:
    """One reading by construction: the later mark is the decimal point.

    The grouping is still checked. ``1,23,4.56`` names a decimal mark
    unambiguously and then groups the rest in a way no convention produces,
    which makes it malformed rather than ambiguous - and a malformed number is
    not one to guess at either.
    """
    decimal_mark = max(",.", key=cleaned.rfind)
    grouping = "." if decimal_mark == "," else ","
    integer_part = cleaned.rsplit(decimal_mark, maxsplit=1)[0].lstrip("+-")

    if not is_well_formed_grouping(integer_part, grouping):
        msg = f"{original!r} groups its digits in a way no convention produces"
        raise AmbiguousNumber(msg)
    return cleaned.replace(grouping, "").replace(decimal_mark, ".")


def _resolve_single_separator(original: str, cleaned: str, mark: str, *, spaced: bool) -> str:
    """Decide what one kind of separator means, by counting the readings.

    Split out because this is the whole of the judgement and deserves to be read
    on its own.
    """
    occurrences = cleaned.count(mark)
    body = cleaned.lstrip("+-")

    # Spaces did the grouping, so whatever is left marks the decimal.
    if spaced:
        if occurrences == 1:
            return cleaned.replace(mark, ".")
        msg = f"grouped with spaces and {occurrences} {mark!r} separators: {original!r}"
        raise AmbiguousNumber(msg)

    # More than one of a kind: there cannot be two decimal marks, so every one
    # of them is grouping. One reading - if the grouping is well formed.
    if occurrences > 1:
        if is_well_formed_grouping(body, mark):
            return cleaned.replace(mark, "")
        msg = f"{original!r} groups its digits in a way no convention produces"
        raise AmbiguousNumber(msg)

    # Exactly one. A three-digit tail is both a thousands group and a fraction;
    # any other tail can only be a fraction.
    if is_well_formed_grouping(body, mark):
        msg = (
            f"{original!r} could be grouping or a decimal mark. "
            f"Print it as a plain decimal, or send it to a person."
        )
        raise AmbiguousNumber(msg)
    # A comma here is the decimal mark, so it has to become a point before
    # Decimal sees it. Forgetting that turned every resolvable European number
    # into a parse failure that looked like a refusal.
    return cleaned.replace(mark, ".")


def coerce_printed_numbers(payload: object, fields: tuple[str, ...]) -> object:
    """Normalise the named fields of a raw payload, before any of it validates.

    Called from a ``mode="before"`` model validator on the contracts that read
    document content, and **deliberately not** attached to the ``Money``,
    ``UnitPrice`` or ``Quantity`` annotations themselves.

    Attaching it to the annotations is the obvious design and it breaks the
    system. A ``BeforeValidator`` makes pydantic widen the field's JSON schema
    and emit ``decimal_places`` and ``max_digits`` as raw keywords - which are
    not JSON Schema - and the live API rejects the resulting
    ``InvoiceExtraction`` schema outright with ``400 Schema is too complex``.
    That was measured, not guessed: the schema came out *smaller* by every count
    available here (76 nodes against 104, 7204 bytes against 7223, the same 19
    properties) and was still refused. Anything that changes the schema the
    model is given has to be probed against the API, because nothing local
    predicts it.

    Scoping it here is also the more honest boundary. A grouped thousands
    separator is a thing a *document* does. An internal contract assembling a
    ``Money`` from code has no business accepting ``"1,234.56"``, and now it
    does not.

    Args:
        payload: Whatever was handed to the model. Anything that is not a
            mapping is returned untouched, so pydantic reports the real error.
        fields: The keys to normalise, if present and if they are strings.

    Returns:
        The payload, with those keys parsed into ``Decimal``.

    Raises:
        AmbiguousNumber: A value has more than one reading. Surfaces as a
            validation error, which the extraction path already turns into
            ``ExtractionError`` and routes to a person.
    """
    if not isinstance(payload, dict):
        return payload
    data = cast("dict[str, Any]", payload)
    updated = {
        key: normalise_decimal_text(value)
        for key, value in data.items()
        if key in fields and isinstance(value, str)
    }
    return {**data, **updated} if updated else data


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

A string arriving here goes through :func:`normalise_decimal_text` first, because
a model asked to transcribe what is printed returns ``"272,100.00"`` - which is
what the page says and which ``Decimal`` will not parse. A live run lost a whole
extraction to exactly that, and the corpus it had been passing on prints grouped
thousands too: it had been surviving on which way each model happened to round a
formatting decision.
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
