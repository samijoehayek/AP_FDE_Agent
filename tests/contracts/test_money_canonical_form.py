"""Money has exactly one serialised form per value.

This exists because of the audit chain. Each event is hashed over a
serialisation, so two representations of the same amount would hash differently
and the chain would report tampering where none happened. A chain that cries
wolf is a chain that gets ignored, which is worse than not having one.

The tests walk the contracts package rather than naming fields, so a money field
added later is covered without anyone remembering to come back here.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import date
from decimal import Decimal
from typing import Any, cast, get_args, get_origin

import pytest
import sqlalchemy as sa
from pydantic import BaseModel, TypeAdapter, ValidationError

import ap_agent.contracts as contracts_pkg
from ap_agent.contracts.audit import AuditEvent, SystemActor, utc_now
from ap_agent.contracts.common import (
    COST_PLACES,
    MAX_MONEY_DIGITS,
    MONEY_PLACES,
    AmbiguousNumber,
    Money,
    normalise_decimal_text,
)
from ap_agent.contracts.enums import AuditEventType
from ap_agent.contracts.invoice import InvoiceExtraction, LineItem
from ap_agent.db.models import AuditEventRow, Invoice

MONEY_FIELD_NAMES = ("subtotal", "tax_total", "total", "extended_price", "amount", "max_amount")


def _extraction(**overrides: Any) -> InvoiceExtraction:
    payload: dict[str, Any] = {
        "vendor_name": "Acme",
        "invoice_number": "INV-1",
        "invoice_date": date(2026, 1, 1),
        "currency": "USD",
        "subtotal": Decimal("100.00"),
        "tax_total": Decimal("20.00"),
        "total": Decimal("120.00"),
        "line_items": [],
    }
    payload.update(overrides)
    return InvoiceExtraction(**payload)


def _audit(**overrides: Any) -> AuditEvent:
    payload: dict[str, Any] = {
        "invoice_id": "i",
        "run_id": "r",
        "step_seq": 0,
        "ts_utc": utc_now(),
        "actor": SystemActor(),
        "event_type": AuditEventType.MODEL_CALL,
    }
    payload.update(overrides)
    return AuditEvent(**payload)


# --- the property the audit chain depends on --------------------------------


@pytest.mark.parametrize("raw", ["1676976", "1676976.0", "1676976.00", "1.676976E+6"])
def test_equal_amounts_serialise_identically(raw: str) -> None:
    """The whole point: same value in, same bytes out, whatever the input form."""
    extraction = _extraction(subtotal=Decimal(raw), tax_total=Decimal(0), total=Decimal(raw))
    assert extraction.model_dump(mode="json")["subtotal"] == "1676976.00"


def test_two_extractions_of_the_same_invoice_are_byte_identical() -> None:
    """What a hash of the extraction would actually see."""
    first = _extraction(subtotal=Decimal(100), tax_total=Decimal(20), total=Decimal(120))
    second = _extraction(
        subtotal=Decimal("100.00"), tax_total=Decimal("20.0"), total=Decimal("120.000")
    )
    assert first.model_dump_json() == second.model_dump_json()


def test_negative_zero_normalises() -> None:
    """Decimal('-0.00') equals zero but carries a sign into the serialisation."""
    assert _extraction(subtotal=Decimal("-0.00")).model_dump(mode="json")["subtotal"] == "0.00"


def test_a_json_round_trip_is_stable() -> None:
    """Reload what was written and the bytes must not move again."""
    once = _extraction(subtotal=Decimal(1676976)).model_dump_json()
    twice = InvoiceExtraction.model_validate_json(once).model_dump_json()
    assert once == twice


def test_the_whole_extraction_is_byte_stable_not_just_money() -> None:
    """Every Decimal in the payload, because the chain hashes all of it.

    Canonicalising money alone would have left quantity, unit_price and tax_rate
    free to vary. Three live runs of the same invoice returned quantity as
    '9.00', '9' and '9.00' - stable twice by luck, which is the worst kind of
    bug: one that passes most of the time.
    """
    loose = _extraction(
        subtotal=Decimal(100),
        tax_total=Decimal(20),
        total=Decimal(120),
        line_items=[
            {
                "description": "Widget",
                "quantity": Decimal(9),
                "unit": "pcs",
                "unit_price": Decimal(74120),
                "extended_price": Decimal(100),
                "tax_rate": Decimal("0.1"),
            }
        ],
    )
    padded = _extraction(
        subtotal=Decimal("100.00"),
        tax_total=Decimal("20.000"),
        total=Decimal("120.0"),
        line_items=[
            {
                "description": "Widget",
                "quantity": Decimal("9.000000"),
                "unit": "pcs",
                "unit_price": Decimal("74120.00"),
                "extended_price": Decimal("100.0"),
                "tax_rate": Decimal("0.10000"),
            }
        ],
    )
    assert loose.model_dump_json() == padded.model_dump_json()


@pytest.mark.parametrize(
    ("field", "loose", "padded"),
    [
        ("quantity", Decimal(9), Decimal("9.000000")),
        ("unit_price", Decimal(74120), Decimal("74120.00")),
        ("tax_rate", Decimal("0.1"), Decimal("0.10000")),
        ("extended_price", Decimal(100), Decimal("100.00")),
    ],
)
def test_every_line_item_decimal_canonicalises(field: str, loose: Decimal, padded: Decimal) -> None:
    base: dict[str, Any] = {
        "description": "Widget",
        "quantity": Decimal(1),
        "unit": "pcs",
        "unit_price": Decimal(1),
        "extended_price": Decimal(1),
        "tax_rate": Decimal(0),
    }
    a = LineItem.model_validate({**base, field: loose}).model_dump_json()
    b = LineItem.model_validate({**base, field: padded}).model_dump_json()
    assert a == b


# --- what it refuses --------------------------------------------------------


@pytest.mark.parametrize("raw", ["120.005", "0.001"])
def test_more_places_than_the_scale_are_refused_not_rounded(raw: str) -> None:
    """Silently rounding an invoice total is how a system loses money quietly."""
    with pytest.raises(ValidationError):
        _extraction(subtotal=Decimal(raw))


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_amounts_are_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        _extraction(subtotal=Decimal(raw))


def test_an_amount_too_large_to_scale_is_refused() -> None:
    """Padding to two places must not push a value past the declared digits."""
    with pytest.raises(ValidationError):
        _extraction(subtotal=Decimal("1E+18"))


# --- costs are not invoice amounts ------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("0.027", "0.027000"), ("0.1619", "0.161900")])
def test_a_real_api_cost_can_be_recorded(raw: str, expected: str) -> None:
    """One extraction costs about $0.027. Two decimal places rejected it outright."""
    assert _audit(cost_usd=Decimal(raw)).model_dump(mode="json")["cost_usd"] == expected


def test_a_cost_finer_than_the_scale_is_refused() -> None:
    with pytest.raises(ValidationError):
        _audit(cost_usd=Decimal("0.0000001"))


# --- nothing drifts ---------------------------------------------------------


def _contract_models() -> list[type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    for info in pkgutil.iter_modules(contracts_pkg.__path__):
        module = importlib.import_module(f"{contracts_pkg.__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj is not BaseModel:
                models[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return sorted(models.values(), key=lambda m: m.__qualname__)


@pytest.mark.parametrize("model", _contract_models(), ids=lambda m: m.__qualname__)
def test_every_money_field_is_decimal_and_constrained(model: type[BaseModel]) -> None:
    """A money field that slipped in as float or bare Decimal would not canonicalise."""
    for name, field in model.model_fields.items():
        if name not in MONEY_FIELD_NAMES:
            continue
        annotation: Any = field.annotation
        candidates = get_args(annotation) if get_origin(annotation) else (annotation,)
        assert float not in candidates, f"{model.__qualname__}.{name} is a float"
        assert Decimal in candidates, f"{model.__qualname__}.{name} is not a Decimal"
        assert field.metadata, f"{model.__qualname__}.{name} carries no constraints"


def test_the_database_scale_matches_the_contract() -> None:
    """A column narrower than the contract would round on write, outside validation."""
    expected = {
        Invoice.__table__.c.total: MONEY_PLACES,
        AuditEventRow.__table__.c.cost_usd: COST_PLACES,
    }
    for column, places in expected.items():
        column_type = column.type
        assert isinstance(column_type, sa.Numeric), column.name
        numeric = cast("sa.Numeric[Decimal]", column_type)
        assert numeric.scale == places, column.name
        assert numeric.precision == MAX_MONEY_DIGITS, column.name


def Money_adapter() -> TypeAdapter[Decimal]:  # noqa: N802 - it builds the Money type
    """A validator for the bare ``Money`` annotation, for pass-through tests."""
    return TypeAdapter(Money)


# --- numbers as documents print them ----------------------------------------
#
# A live run lost a whole extraction to `'272,100.00'`: what the page said, what
# the model faithfully returned, and what `Decimal` refuses. The corpus it had
# been passing on prints grouped thousands too - it had been surviving on which
# way each model happened to round a formatting decision.


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("272,100.00", Decimal("272100.00")),
        ("1,67,560.00", Decimal("167560.00")),  # Indian lakh grouping
        ("1.234,56", Decimal("1234.56")),  # European
        ("1 234,56", Decimal("1234.56")),  # French, space-grouped
        ("1234.56", Decimal("1234.56")),
        ("-45.00", Decimal("-45.00")),
        ("1 234 567", Decimal(1234567)),
        ("1234", Decimal(1234)),
        ("$1,234.50", Decimal("1234.50")),
        ("  272,100.00  ", Decimal("272100.00")),
        # Six places, because a unit price is stored at six and a truth file
        # round-trips through this parser. A rule that refused these would make
        # the generated fixture unreadable by its own contract.
        ("9.250000", Decimal("9.250000")),
    ],
)
def test_a_printed_number_normalises(printed: str, expected: Decimal) -> None:
    assert normalise_decimal_text(printed) == expected


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        # Two trailing digits can only be a decimal mark: no convention groups
        # in twos at the end. Supersedes an earlier rule that refused any lone
        # comma - refusing a string with exactly one reading buys no safety.
        ("1,23", Decimal("1.23")),
        # Two identical separators can only be grouping: there is no such thing
        # as two decimal marks.
        ("1,234,567", Decimal(1234567)),
        ("1.234.567", Decimal(1234567)),
        ("12.345.678", Decimal(12345678)),
        # Four trailing digits are not a thousands group either.
        ("1,2345", Decimal("1.2345")),
        # Lakh grouping with the decimal mark named by the other separator.
        ("1,23,456.00", Decimal("123456.00")),
    ],
)
def test_a_number_with_exactly_one_reading_resolves(printed: str, expected: Decimal) -> None:
    """The rule in one sentence: refuse only when more than one reading exists.

    Each of these has exactly one, so each resolves. Several of them were
    refused by the first version of this rule, which was blunter than the
    problem - it declined a lone comma outright, sending ``1,23`` to a person
    even though 1.23 is the only thing it can mean.
    """
    assert normalise_decimal_text(printed) == expected


@pytest.mark.parametrize(
    "printed",
    [
        # One separator, a three-digit tail: a thousands group or a fraction,
        # and nothing in the string says which.
        "1,234",
        "1.234",
        # Grouping no convention produces, and no decimal reading left.
        "1.2.3",
        "",
        "abc",
    ],
)
def test_a_number_with_more_than_one_reading_refuses(printed: str) -> None:
    """``1,234`` is 1234 to one reader and 1.234 to another.

    A factor of a thousand on an invoice this system would then pay, with
    nothing downstream able to tell. A refusal costs one trip to a person.
    """
    with pytest.raises(AmbiguousNumber):
        normalise_decimal_text(printed)


def test_a_decimal_passes_through_untouched() -> None:
    """Only strings are parsed. A Decimal has already answered the question."""
    value = Decimal("1234.56")
    assert Money_adapter().validate_python(value) == value


def test_a_grouped_string_reaches_a_money_field() -> None:
    """The path that actually failed live, end to end through the contract."""
    line = LineItem(
        description="27in IPS Monitor",
        quantity="11",  # type: ignore[arg-type]
        unit_price="18,900.00",  # type: ignore[arg-type]
        extended_price="207,900.00",  # type: ignore[arg-type]
    )
    assert line.unit_price == Decimal("18900.000000")
    assert line.extended_price == Decimal("207900.00")


def test_an_ambiguous_string_fails_validation_rather_than_guessing() -> None:
    """It surfaces as a ValidationError, which the extraction path already routes."""
    with pytest.raises(ValidationError):
        LineItem(
            description="x",
            quantity="1",  # type: ignore[arg-type]
            unit_price="1,234",  # type: ignore[arg-type]
            extended_price="1.00",  # type: ignore[arg-type]
        )


def test_normalising_does_not_defeat_the_refusal_to_round() -> None:
    """Grouping is removed; precision is still never silently lost."""
    with pytest.raises(ValidationError):
        LineItem(
            description="x",
            quantity="1",  # type: ignore[arg-type]
            unit_price="1.00",  # type: ignore[arg-type]
            extended_price="1,234.5678",  # type: ignore[arg-type]
        )


def test_an_empty_unit_is_allowed_and_means_absent() -> None:
    """Real invoice lines often print no unit of measure, and one lost a run."""
    line = LineItem(
        description="x",
        quantity=Decimal(1),
        unit_price=Decimal("1.00"),
        extended_price=Decimal("1.00"),
    )
    assert line.unit == ""


def test_the_schema_the_model_sees_keeps_unit_a_plain_string() -> None:
    """`str | None` would add an anyOf branch, and this schema has no headroom.

    A previous attempt to add optional fields was rejected outright by the API
    with ``400 Schema is too complex``.
    """
    schema = InvoiceExtraction.model_json_schema()
    unit = schema["$defs"]["LineItem"]["properties"]["unit"]
    assert unit["type"] == "string"
    assert "anyOf" not in unit


# --- the schema the model is actually given ---------------------------------
#
# Structured outputs enforces a complexity budget that nothing local predicts.
# A change that made this schema *smaller* by every count available here - 76
# nodes against 104, 7204 bytes against 7223, the same 19 properties - was still
# refused with `400 Schema is too complex`, and cost a live call to discover.
# These pin the shape that is known to be accepted.

SCHEMA = InvoiceExtraction.model_json_schema()

JSON_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "anyOf",
        "allOf",
        "oneOf",
        "$ref",
        "$defs",
        "enum",
        "const",
        "default",
        "description",
        "title",
        "format",
        "pattern",
        "additionalProperties",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "discriminator",
    }
)


def _keys(node: object) -> set[str]:
    """Every key appearing anywhere in the schema."""
    found: set[str] = set()
    if isinstance(node, dict):
        mapping = cast("dict[str, Any]", node)
        found |= set(mapping)
        for value in mapping.values():
            found |= _keys(value)
    elif isinstance(node, list):
        for item in cast("list[Any]", node):
            found |= _keys(item)
    return found


def test_the_schema_declares_no_keyword_json_schema_does_not_have() -> None:
    """`decimal_places` and `max_digits` are pydantic's, not JSON Schema's.

    A `BeforeValidator` on a money annotation makes pydantic emit them as raw
    keywords, and the API refuses the whole schema. This is the assertion that
    would have caught it - the coercion belongs in a model-level validator,
    where it changes no annotation and therefore no schema.
    """
    stray = _keys(SCHEMA) - JSON_SCHEMA_KEYWORDS - set(SCHEMA.get("properties", {}))
    stray -= set(SCHEMA.get("$defs", {}))
    for definition in SCHEMA.get("$defs", {}).values():
        stray -= set(cast("dict[str, Any]", definition).get("properties", {}))
    assert stray == set(), f"schema carries non-JSON-Schema keywords: {sorted(stray)}"


def test_the_property_count_stays_inside_the_budget() -> None:
    """19 is verified accepted. 20 was rejected at fewer bytes than 17 passed at.

    The limit is a property count, not a byte count, and it is not documented.
    Probe it against the live API before raising this.
    """
    assert len(SCHEMA["properties"]) <= 19
