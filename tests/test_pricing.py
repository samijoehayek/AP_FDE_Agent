"""What a model call cost, and what happens when nobody knows.

The distinction this file is really about: **unpriced is not free**. A model the
price list has never heard of must produce ``None``, not zero - zero is a claim
that the call cost nothing, and a run full of zeroes looks like a cheap run
rather than an unmeasured one.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ap_agent.pricing import PricingError, load_price_list, pricing_version

ONE_MODEL = """
version: "test-1"
models:
  tiny-model:
    input_per_million: "2.00"
    output_per_million: "10.00"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "model_pricing.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- the shipped list -------------------------------------------------------


def test_the_shipped_list_prices_both_extraction_seats() -> None:
    """An unpriced seat means every extraction row in the trail says None."""
    prices = load_price_list()
    assert "claude-sonnet-5" in prices.models
    assert "claude-haiku-4-5-20251001" in prices.models


def test_the_alias_is_priced_as_well_as_the_dated_id() -> None:
    """Settings default to the alias; the API reports the dated id.

    Both appear in a trail, so both have to be priced or half the rows lose
    their cost for no reason a reader could work out.
    """
    prices = load_price_list()
    assert "claude-haiku-4-5" in prices.models


def test_the_shipped_list_is_versioned() -> None:
    """A cost that cannot be dated cannot be re-explained after prices move."""
    assert pricing_version()


def test_the_shipped_sonnet_price_is_the_verified_one() -> None:
    """$3/$15 was the September increase Anthropic cancelled, recalled not read.

    Pinned because the wrong figure was wrong by 50% and nothing in a trail
    would have looked odd.
    """
    price = load_price_list().models["claude-sonnet-5"]
    assert price.input_per_million == Decimal("2.00")
    assert price.output_per_million == Decimal("10.00")


# --- arithmetic -------------------------------------------------------------


def test_a_known_model_costs_tokens_times_rate(tmp_path: Path) -> None:
    prices = load_price_list(_write(tmp_path, ONE_MODEL))

    # 1,000,000 in at $2 and 1,000,000 out at $10.
    assert prices.cost_of("tiny-model", 1_000_000, 1_000_000) == Decimal("12.000000")


def test_a_realistic_call_keeps_six_places(tmp_path: Path) -> None:
    """Two places would round a $0.023 extraction to $0.02, and most of a run to nothing."""
    prices = load_price_list(_write(tmp_path, ONE_MODEL))

    cost = prices.cost_of("tiny-model", 7211, 1326)

    assert cost is not None
    assert cost == Decimal("0.027682")
    assert cost.as_tuple().exponent == -6


def test_a_free_call_is_zero_not_none(tmp_path: Path) -> None:
    """Zero tokens really did cost nothing. That is a different answer from unknown."""
    prices = load_price_list(_write(tmp_path, ONE_MODEL))
    assert prices.cost_of("tiny-model", 0, 0) == Decimal(0)


# --- not knowing ------------------------------------------------------------


def test_an_unpriced_model_is_none_not_zero(tmp_path: Path) -> None:
    """The whole point. Zero would claim the call was free."""
    prices = load_price_list(_write(tmp_path, ONE_MODEL))
    assert prices.cost_of("some-model-nobody-listed", 1000, 1000) is None


def test_an_unpriced_model_does_not_crash(tmp_path: Path) -> None:
    """Losing the audit row for a call that really happened would be worse."""
    prices = load_price_list(_write(tmp_path, ONE_MODEL))
    assert prices.cost_of(None, 1000, 1000) is None


# --- the file failing -------------------------------------------------------


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PricingError, match="no model price list"):
        load_price_list(tmp_path / "absent.yaml")


def test_a_list_with_no_version_is_refused(tmp_path: Path) -> None:
    """A cost nobody can date is a cost nobody can defend."""
    body = "models:\n  tiny-model:\n    input_per_million: '1'\n    output_per_million: '2'\n"
    with pytest.raises(PricingError, match="declares no version"):
        load_price_list(_write(tmp_path, body))


def test_a_model_missing_a_rate_names_the_model(tmp_path: Path) -> None:
    body = "version: 'x'\nmodels:\n  half-priced:\n    input_per_million: '1'\n"
    with pytest.raises(PricingError, match="half-priced"):
        load_price_list(_write(tmp_path, body))


def test_broken_yaml_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PricingError, match="not valid YAML"):
        load_price_list(_write(tmp_path, "models: [oops\n"))
