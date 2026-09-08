"""Assertions over the tool package as a whole.

Two of these are security controls rather than hygiene checks: no tool is
exposed to a model, and the tools that would make a mistake unrecoverable do not
exist. Both are stated in ``CLAUDE.md``; this is where they are enforced.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType
from typing import cast, get_type_hints

import pytest
from pydantic import BaseModel

import ap_agent.tools as tools_pkg
from ap_agent.tools import FORBIDDEN_TOOL_NAMES, TOOL_MODULE_NAMES
from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller

EXTERNAL_WRITE_EFFECTS = frozenset({SideEffect.ERP_WRITE, SideEffect.NOTIFICATION})


def _module(name: str) -> ModuleType:
    return importlib.import_module(f"{tools_pkg.__name__}.{name}")


def _caller(module: ModuleType) -> object:
    return cast("object", module.CALLER)


def _side_effects(module: ModuleType) -> tuple[object, ...]:
    return cast("tuple[object, ...]", module.SIDE_EFFECTS)


def _requires_key(module: ModuleType) -> object:
    return cast("object", module.REQUIRES_IDEMPOTENCY_KEY)


def _discovered_module_names() -> set[str]:
    return {
        info.name
        for info in pkgutil.iter_modules(tools_pkg.__path__)
        if not info.name.startswith("_") and info.name != "base"
    }


# --- the controls -----------------------------------------------------------


def test_no_forbidden_tool_exists() -> None:
    """Absence is the control. A tool that cannot be called cannot be misused."""
    present = _discovered_module_names() & FORBIDDEN_TOOL_NAMES
    assert not present, f"forbidden tool modules exist: {sorted(present)}"


def test_no_module_deletes_anything() -> None:
    deleters = {name for name in _discovered_module_names() if name.startswith("delete_")}
    assert not deleters, f"deletion tools exist: {sorted(deleters)}"


def test_no_module_pays_or_edits_a_vendor() -> None:
    """A name check, not a semantic one - but it catches the obvious regression."""
    banned_prefixes = ("pay_", "update_vendor", "update_bank", "transfer_", "wire_")
    offenders = {name for name in _discovered_module_names() if name.startswith(banned_prefixes)}
    assert not offenders, f"payment or vendor-mutation tools exist: {sorted(offenders)}"


@pytest.mark.parametrize("name", TOOL_MODULE_NAMES)
def test_no_tool_is_exposed_to_a_model(name: str) -> None:
    """Rule 1: neither LLM seat has tools. Widening this is an architecture change."""
    assert _caller(_module(name)) is ToolCaller.CODE


# --- declared metadata ------------------------------------------------------


def test_the_registry_matches_what_is_on_disk() -> None:
    assert _discovered_module_names() == set(TOOL_MODULE_NAMES)


@pytest.mark.parametrize("name", TOOL_MODULE_NAMES)
def test_every_tool_declares_its_metadata(name: str) -> None:
    module = _module(name)
    effects = _side_effects(module)
    assert isinstance(_caller(module), ToolCaller)
    assert effects, f"{name} declares no side effects, not even NONE"
    assert all(isinstance(effect, SideEffect) for effect in effects)
    assert isinstance(_requires_key(module), bool)


@pytest.mark.parametrize("name", TOOL_MODULE_NAMES)
def test_every_tool_documents_its_caller_and_side_effects(name: str) -> None:
    doc = _module(name).__doc__ or ""
    assert "Caller:" in doc, f"{name} does not state its caller"
    assert "Side effects:" in doc, f"{name} does not state its side effects"


@pytest.mark.parametrize("name", TOOL_MODULE_NAMES)
def test_every_tool_has_typed_input_and_output_models(name: str) -> None:
    module = _module(name)
    cls = "".join(part.capitalize() for part in name.split("_"))
    for suffix in ("Input", "Output"):
        model = getattr(module, f"{cls}{suffix}")
        assert issubclass(model, BaseModel)
        assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("name", TOOL_MODULE_NAMES)
def test_the_tool_function_takes_its_input_model(name: str) -> None:
    module = _module(name)
    cls = "".join(part.capitalize() for part in name.split("_"))
    function = getattr(module, name)

    # Every module uses `from __future__ import annotations`, so the raw
    # signature holds strings; resolve them before comparing.
    hints = get_type_hints(function)
    (parameter,) = inspect.signature(function).parameters
    assert hints[parameter] is getattr(module, f"{cls}Input")
    assert hints["return"] is getattr(module, f"{cls}Output")


# --- idempotency ------------------------------------------------------------


@pytest.mark.parametrize("name", TOOL_MODULE_NAMES)
def test_external_writes_require_an_idempotency_key(name: str) -> None:
    """Rule 5. A retried create_bill must not create a second bill."""
    module = _module(name)
    writes_externally = bool(set(module.SIDE_EFFECTS) & EXTERNAL_WRITE_EFFECTS)
    assert writes_externally == module.REQUIRES_IDEMPOTENCY_KEY, (
        f"{name}: side effects {module.SIDE_EFFECTS} disagree with "
        f"REQUIRES_IDEMPOTENCY_KEY={module.REQUIRES_IDEMPOTENCY_KEY}"
    )


@pytest.mark.parametrize("name", TOOL_MODULE_NAMES)
def test_idempotent_tools_carry_the_key_on_their_input_model(name: str) -> None:
    module = _module(name)
    cls = "".join(part.capitalize() for part in name.split("_"))
    model = getattr(module, f"{cls}Input")
    if _requires_key(module):
        assert issubclass(model, IdempotentToolInput)
        assert "idempotency_key" in model.model_fields
    else:
        assert "idempotency_key" not in model.model_fields


# --- stubs ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(set(TOOL_MODULE_NAMES) - {"ingest_document"}))
def test_unimplemented_tools_fail_loudly(name: str) -> None:
    """A stub that returns a plausible default is worse than one that raises."""
    module = _module(name)
    cls = "".join(part.capitalize() for part in name.split("_"))
    with pytest.raises(NotImplementedError):
        getattr(module, name)(getattr(module, f"{cls}Input").model_construct())
