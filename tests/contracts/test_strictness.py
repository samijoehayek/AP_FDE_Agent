"""Cross-cutting assertions over every contract model.

These are the tests that keep the architecture honest as the schema grows. They
walk the package rather than naming models, so a contract added next month is
covered the moment it is imported.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel

import ap_agent.contracts as contracts_pkg


def _all_contract_models() -> list[type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    for info in pkgutil.iter_modules(contracts_pkg.__path__):
        module = importlib.import_module(f"{contracts_pkg.__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj is not BaseModel:
                models[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return sorted(models.values(), key=lambda m: m.__qualname__)


CONTRACT_MODELS = _all_contract_models()

BANK_DETAIL_PATTERN = re.compile(
    r"iban|bic|swift|sort_code|routing|account_number|account_no|bank_account|"
    r"card_number|beneficiary",
    re.IGNORECASE,
)


def test_the_walk_found_the_models() -> None:
    assert len(CONTRACT_MODELS) >= 15, [m.__qualname__ for m in CONTRACT_MODELS]


@pytest.mark.parametrize("model", CONTRACT_MODELS, ids=lambda m: m.__qualname__)
def test_every_contract_forbids_extra_fields(model: type[BaseModel]) -> None:
    """extra="forbid" is the control that stops unmodelled data crossing a boundary."""
    assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("model", CONTRACT_MODELS, ids=lambda m: m.__qualname__)
def test_no_contract_carries_bank_details(model: type[BaseModel]) -> None:
    """Rule 2 of CLAUDE.md, enforced rather than documented.

    Bank details have exactly one home - the vendor master - and are reduced to a
    boolean server-side. A field here would defeat every other control.
    """
    offending = [name for name in model.model_fields if BANK_DETAIL_PATTERN.search(name)]
    assert not offending, f"{model.__qualname__} declares bank-detail fields: {offending}"


@pytest.mark.parametrize("model", CONTRACT_MODELS, ids=lambda m: m.__qualname__)
def test_no_contract_uses_float_for_money(model: type[BaseModel]) -> None:
    """Money is Decimal. A float tolerance check fails in ways that look like vendor fraud."""
    money_names = re.compile(r"total|price|amount|subtotal|tax_total|cost", re.IGNORECASE)
    for name, field in model.model_fields.items():
        if not money_names.search(name):
            continue
        annotation: Any = field.annotation
        candidates = get_args(annotation) if get_origin(annotation) else (annotation,)
        assert float not in candidates, f"{model.__qualname__}.{name} is a float"
