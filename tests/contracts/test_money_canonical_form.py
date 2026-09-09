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
from pydantic import BaseModel, ValidationError

import ap_agent.contracts as contracts_pkg
from ap_agent.contracts.audit import AuditEvent, SystemActor, utc_now
from ap_agent.contracts.common import (
    COST_PLACES,
    MAX_MONEY_DIGITS,
    MONEY_PLACES,
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
