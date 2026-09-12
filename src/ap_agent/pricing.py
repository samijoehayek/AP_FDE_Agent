"""What a model call cost, from a versioned price list.

Token prices live in ``config/model_pricing.yaml`` rather than in this file, for
the same reason the tolerances live in ``config/guardrails.v1.yaml``: they change
on someone else's schedule, and an audit row claiming a cost has to stay
explainable after they do. The row records which pricing version produced it, so
"why does this invoice say $0.027" is answerable next year by reading the file
that was in force.

**An unknown model costs ``None``, never zero and never a crash.** Zero would be
a claim - that the call was free - and a crash would lose the audit row for a
call that really happened. ``None`` says the one true thing: nobody priced it.
The gap is logged so it can be closed.

Nothing here decides anything. It multiplies two numbers and quantises the
result to the six places ``CostUsd`` carries, because one extraction costs about
$0.027 and two decimal places would round most of a run's cost to nothing.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import yaml

from ap_agent.config import get_settings
from ap_agent.errors import ConfigurationError
from ap_agent.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(__name__)

PER_MILLION = Decimal(1_000_000)
COST_PLACES = Decimal("0.000001")
"""Six places, matching ``CostUsd`` and the ``audit_events.cost_usd`` column."""

UNKNOWN_PRICING_VERSION = "unpriced"
"""Recorded on a row whose model is not in the price list."""


class PricingError(ConfigurationError):
    """The price list is missing or malformed.

    Distinct from "this model is not priced", which is ``None`` and a warning.
    This is the file itself being unreadable, which affects every call.
    """


class ModelPrice:
    """One model's input and output rates, in USD per million tokens."""

    __slots__ = ("input_per_million", "output_per_million")

    def __init__(self, input_per_million: Decimal, output_per_million: Decimal) -> None:
        self.input_per_million = input_per_million
        self.output_per_million = output_per_million

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Return the USD cost of one call, quantised to six places."""
        total = (
            Decimal(input_tokens) * self.input_per_million
            + Decimal(output_tokens) * self.output_per_million
        ) / PER_MILLION
        return total.quantize(COST_PLACES)


class PriceList:
    """A loaded price list, and the version string that names it."""

    __slots__ = ("models", "version")

    def __init__(self, version: str, models: dict[str, ModelPrice]) -> None:
        self.version = version
        self.models = models

    def cost_of(
        self, model_id: str | None, input_tokens: int, output_tokens: int
    ) -> Decimal | None:
        """Return what this call cost, or None when the model is not priced.

        Args:
            model_id: As reported by the API, not as requested - an alias that
                resolved to a dated model must be priced as what actually ran.
            input_tokens: Tokens sent.
            output_tokens: Tokens returned.

        Returns:
            The cost in USD to six places, or ``None`` when nothing prices this
            model. ``None`` is not zero: it means unknown, and a reader of the
            trail must be able to tell those apart.
        """
        if model_id is None:
            return None
        price = self.models.get(model_id)
        if price is None:
            log.warning("model_not_priced", model_id=model_id, pricing_version=self.version)
            return None
        return price.cost(input_tokens, output_tokens)


def _price_of(model_id: str, record: dict[str, Any]) -> ModelPrice:
    """Build one model's rates, naming the model if a field is missing."""
    missing = [key for key in ("input_per_million", "output_per_million") if key not in record]
    if missing:
        msg = f"model_pricing.yaml: {model_id} is missing {', '.join(missing)}"
        raise PricingError(msg)
    try:
        return ModelPrice(
            input_per_million=Decimal(str(record["input_per_million"])),
            output_per_million=Decimal(str(record["output_per_million"])),
        )
    except ArithmeticError as exc:
        msg = f"model_pricing.yaml: {model_id} has a rate that is not a number"
        raise PricingError(msg) from exc


@lru_cache(maxsize=4)
def load_price_list(path: Path | None = None) -> PriceList:
    """Load the price list, once per path per process.

    Cached because a run must price every call from the same list: a file that
    changed mid-run would make two rows in one trail incomparable for no
    recorded reason.

    Raises:
        PricingError: The file is absent, unparseable, or has no version.
    """
    source = path or get_settings().model_pricing_path
    if not source.is_file():
        msg = f"no model price list at {source}"
        raise PricingError(msg)

    try:
        loaded: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"{source.name} is not valid YAML: {exc}"
        raise PricingError(msg) from exc

    if not isinstance(loaded, dict):
        msg = f"{source.name} must be a mapping with `version` and `models`"
        raise PricingError(msg)

    document = cast("dict[str, Any]", loaded)
    version = document.get("version")
    if not isinstance(version, str) or not version:
        msg = f"{source.name} declares no version; a cost that cannot be dated cannot be explained"
        raise PricingError(msg)

    raw_models: Any = document.get("models")
    if not isinstance(raw_models, dict):
        msg = f"{source.name} declares no models"
        raise PricingError(msg)

    models = {
        str(model_id): _price_of(str(model_id), cast("dict[str, Any]", record))
        for model_id, record in cast("dict[str, Any]", raw_models).items()
        if isinstance(record, dict)
    }
    return PriceList(version=version, models=models)


def cost_usd(
    model_id: str | None, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    """What one model call cost, or None if it cannot be priced.

    The convenience the audit path actually calls. Missing token counts produce
    ``None`` rather than a cost computed from zero, which would understate a
    call that really happened.
    """
    if input_tokens is None or output_tokens is None:
        return None
    return load_price_list().cost_of(model_id, input_tokens, output_tokens)


def pricing_version() -> str:
    """The version string the current price list carries."""
    return load_price_list().version


__all__ = [
    "COST_PLACES",
    "UNKNOWN_PRICING_VERSION",
    "ModelPrice",
    "PriceList",
    "PricingError",
    "cost_usd",
    "load_price_list",
    "pricing_version",
]
