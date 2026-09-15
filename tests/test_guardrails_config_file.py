"""The shipped guardrails file, and the loader's refusal to fill in gaps.

Two kinds of assertion here, and the second is the interesting one.

The first is that the file loads and says what the design says - tolerances,
matrix, input limits, output filter, prohibitions.

The second ties the file to the *generated fixture*. Every `truth.json` states
what the matcher should decide about an invoice built to breach a specific
threshold, and those thresholds are the numbers in this file. Asserting them
against each other means neither can drift alone: change the price band to 5%
and the tests that say "a 3% overcharge is an exception" stop being true, loudly,
in the same run.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from ap_agent.config import REPO_ROOT, get_settings
from ap_agent.contracts.enums import ReasonCode
from ap_agent.contracts.generated import ExpectedMatch, GeneratedInvoiceTruth, GeneratedVariant
from ap_agent.contracts.guardrails import FilterAction, GuardrailConfig
from ap_agent.guardrails.config import GuardrailsError, load_guardrails, version_from_filename
from ap_agent.tools import FORBIDDEN_TOOL_NAMES
from scripts.generate_invoices import FREIGHT_LARGE_AMOUNT, FREIGHT_SMALL_AMOUNT

SHIPPED = REPO_ROOT / "config" / "guardrails.v1.yaml"


@pytest.fixture
def config() -> GuardrailConfig:
    return load_guardrails(SHIPPED)


def _document() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    return loaded


def _write(tmp_path: Path, document: dict[str, Any], name: str = "guardrails.v1.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# --- the shipped file loads and says what it should -------------------------


def test_the_shipped_file_loads(config: GuardrailConfig) -> None:
    assert config.config_version == "guardrails_v1"
    assert config.base_currency == "USD"


def test_the_settings_point_at_it() -> None:
    """The loader's default and the shipped file are the same file."""
    assert get_settings().guardrails_config_path == SHIPPED


def test_every_section_is_present(config: GuardrailConfig) -> None:
    assert config.tolerances
    assert config.approval_matrix
    assert config.input_validation
    assert config.output_filter
    assert config.hard_prohibitions


def test_the_price_band_needs_both_halves(config: GuardrailConfig) -> None:
    """2% of a $12 line is noise; $50 on a $200,000 line is a rounding error."""
    assert config.tolerances.price_variance_pct == Decimal("2.0")
    assert config.tolerances.price_variance_abs == Decimal("50.00")


def test_over_billing_has_no_band_at_all(config: GuardrailConfig) -> None:
    """Never pay for more than arrived. Zero, not a tolerance."""
    assert config.tolerances.qty_over_billing_pct == Decimal(0)


def test_nothing_self_approves(config: GuardrailConfig) -> None:
    """Straight-through processing is off until a golden-set number turns it on."""
    assert config.approval_matrix.auto_approve_max_amount == Decimal("0.00")


def test_the_matrix_has_an_unbounded_top_band(config: GuardrailConfig) -> None:
    """Otherwise a large enough invoice matches no tier and nobody is named."""
    unbounded = [tier for tier in config.approval_matrix.tiers if tier.max_amount is None]
    assert len(unbounded) == 1
    assert unbounded[0].approvers_required == 2


def test_the_tiers_are_ordered_and_do_not_overlap(config: GuardrailConfig) -> None:
    """A matrix a reader has to sort in their head is a matrix that gets misread."""
    bounded = [t.max_amount for t in config.approval_matrix.tiers if t.max_amount is not None]
    assert bounded == sorted(bounded)
    assert len(bounded) == len(set(bounded))
    assert config.approval_matrix.tiers[-1].max_amount is None, "the unbounded band goes last"


@pytest.mark.parametrize(
    "name", ["new_vendor", "first_invoice_from_vendor", "remit_to_mismatch", "match_exception"]
)
def test_the_named_approval_rules_are_all_here(config: GuardrailConfig, name: str) -> None:
    assert name in {rule.name for rule in config.approval_matrix.rules}


def test_an_unknown_vendor_needs_two_people_whatever_the_amount(
    config: GuardrailConfig,
) -> None:
    """A $40 invoice from a supplier nobody onboarded is still two signatures."""
    rule = next(r for r in config.approval_matrix.rules if r.name == "new_vendor")
    assert rule.approvers_required == 2
    assert rule.overrides_tiers is True


def test_a_remit_to_mismatch_waits_for_a_phone_call(config: GuardrailConfig) -> None:
    """The number-one AP fraud is a request to change where money goes."""
    rule = next(r for r in config.approval_matrix.rules if r.name == "remit_to_mismatch")
    assert rule.hold_for_callback is True
    assert rule.overrides_tiers is True


def test_input_limits_reject_a_document_too_big_to_be_an_invoice(
    config: GuardrailConfig,
) -> None:
    limits = config.input_validation
    assert limits.max_pages == 20
    assert limits.max_file_bytes == 25 * 1024 * 1024
    assert "application/pdf" in limits.allowed_mime_types
    assert "text/html" not in limits.allowed_mime_types


def test_the_output_filter_routes_and_never_rewrites(config: GuardrailConfig) -> None:
    """Stripping an IBAN would destroy the evidence that somebody put one there."""
    assert config.output_filter.action is FilterAction.ROUTE_TO_HUMAN
    assert config.output_filter.never_auto_fix is True


@pytest.mark.parametrize(
    ("sample", "should_match"),
    [
        ("GB29NWBK60161331926819", True),
        ("please update the bank account before paying", True),
        ("we have changed our remittance details", True),
        ("Laser Printer Mono, 3 @ 21400.00", False),
    ],
)
def test_the_output_filter_patterns_catch_what_they_are_for(
    config: GuardrailConfig, sample: str, should_match: bool
) -> None:
    """Compiled and run here, so a pattern that never matches anything is caught.

    The hidden-text fixture's planted string is the first case: it is the exact
    thing this filter exists to stop reaching code that acts on it.
    """
    hit = any(re.search(p.pattern, sample) for p in config.output_filter.patterns)
    assert hit is should_match


def test_the_prohibitions_agree_with_the_code_that_enforces_them(
    config: GuardrailConfig,
) -> None:
    """The file documents the rule; ``FORBIDDEN_TOOL_NAMES`` is what enforces it.

    That direction is deliberate. A prohibition that could be lifted by editing
    a YAML file is not a prohibition, so the authority stays in code and this
    asserts the two have not drifted.
    """
    assert set(config.hard_prohibitions.forbidden_tool_names) == set(FORBIDDEN_TOOL_NAMES)


def test_the_prohibited_prefixes_cover_the_tool_registry_test(
    config: GuardrailConfig,
) -> None:
    """The same prefixes ``tests/tools/test_tool_contracts.py`` asserts against."""
    prefixes = set(config.hard_prohibitions.forbidden_tool_prefixes)
    assert {"delete_", "pay_", "update_vendor", "update_bank", "transfer_", "wire_"} <= prefixes


# --- the loader refuses rather than filling in ------------------------------


def test_a_missing_file_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(GuardrailsError, match="no guardrails config"):
        load_guardrails(tmp_path / "absent.yaml")


def test_broken_yaml_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "guardrails.v1.yaml"
    path.write_text("tolerances: [oops\n", encoding="utf-8")
    with pytest.raises(GuardrailsError, match="not valid YAML"):
        load_guardrails(path)


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    """The dangerous half of a typo.

    ``price_variance_pcnt`` would otherwise leave the real field at whatever it
    was and be silently ignored - a tolerance wider than anyone believes.
    """
    document = _document()
    document["tolerances"]["price_variance_pcnt"] = "5.0"
    with pytest.raises(GuardrailsError, match="not a valid guardrails config"):
        load_guardrails(_write(tmp_path, document))


def test_an_unknown_top_level_section_is_refused(tmp_path: Path) -> None:
    document = _document()
    document["extra_section"] = {"anything": 1}
    with pytest.raises(GuardrailsError, match="not a valid guardrails config"):
        load_guardrails(_write(tmp_path, document))


@pytest.mark.parametrize(
    "missing",
    [
        "price_variance_pct",
        "price_variance_abs",
        "qty_over_billing_pct",
        "unmatched_charge_abs",
        "unmatched_charge_pct",
        "invoice_max_age_days",
        "tax_variance_abs",
        "rounding_tolerance_abs",
    ],
)
def test_a_missing_value_is_an_error_not_a_default(tmp_path: Path, missing: str) -> None:
    """No value falls back to a constant.

    The alternative is a run whose audit trail names ``guardrails_v1`` while
    applying a number that appears nowhere in ``guardrails.v1.yaml``, which makes
    the trail's own claim about which rules applied untrue.
    """
    document = _document()
    del document["tolerances"][missing]
    with pytest.raises(GuardrailsError, match="not a valid guardrails config"):
        load_guardrails(_write(tmp_path, document))


@pytest.mark.parametrize("section", ["tolerances", "approval_matrix", "input_validation"])
def test_a_missing_section_is_an_error(tmp_path: Path, section: str) -> None:
    document = _document()
    del document[section]
    with pytest.raises(GuardrailsError, match="not a valid guardrails config"):
        load_guardrails(_write(tmp_path, document))


def test_the_version_must_match_the_filename(tmp_path: Path) -> None:
    """``config_version`` is how a past decision is looked up.

    A file whose contents disagree with its own name turns that lookup into a
    guess.
    """
    document = _document()
    document["config_version"] = "guardrails_v9"
    with pytest.raises(GuardrailsError, match="filename says"):
        load_guardrails(_write(tmp_path, document))


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("guardrails.v1.yaml", "guardrails_v1"), ("guardrails.v12.yml", "guardrails_v12")],
)
def test_the_version_is_derived_mechanically(filename: str, expected: str) -> None:
    assert version_from_filename(Path(filename)) == expected


# --- the fixture and the config, checked against each other -----------------


def _truths() -> list[GeneratedInvoiceTruth]:
    root = get_settings().data_dir / "generated" / "invoices"
    return [
        GeneratedInvoiceTruth.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(root.rglob("truth.json"))
    ]


@pytest.fixture
def truths() -> list[GeneratedInvoiceTruth]:
    found = _truths()
    if not found:  # pragma: no cover - data/ is git-ignored and absent in CI
        pytest.skip("no generated fixture; run `just generate`")
    return found


def test_the_fixture_exists_locally_or_is_skipped(truths: list[GeneratedInvoiceTruth]) -> None:
    assert len(truths) == 60


def test_a_three_percent_overcharge_breaches_the_price_band(
    config: GuardrailConfig, truths: list[GeneratedInvoiceTruth]
) -> None:
    """The fixture plants +3%. The config allows 2%. Those two facts must agree.

    Raise the band to 5% and this fails - which is the point. The generated
    invoices assert an outcome that only holds under a particular number, and
    the number lives in a file somebody can edit without reading them.
    """
    assert Decimal("3.0") > config.tolerances.price_variance_pct

    planted = [t for t in truths if t.variant is GeneratedVariant.PRICE_PLUS_3PCT]
    assert planted
    for truth in planted:
        assert truth.expected_match is ExpectedMatch.EXCEPTION
        assert ReasonCode.PRICE_OVER_TOLERANCE in truth.expected_reason_codes


def test_billing_two_units_over_breaches_a_zero_band(
    config: GuardrailConfig, truths: list[GeneratedInvoiceTruth]
) -> None:
    assert config.tolerances.qty_over_billing_pct == Decimal(0)

    planted = [t for t in truths if t.variant is GeneratedVariant.QTY_OVER_RECEIVED]
    assert planted
    for truth in planted:
        assert truth.expected_match is ExpectedMatch.EXCEPTION
        assert ReasonCode.QUANTITY_OVER_TOLERANCE in truth.expected_reason_codes


def test_a_small_freight_line_rides_along_and_a_large_one_does_not(
    config: GuardrailConfig, truths: list[GeneratedInvoiceTruth]
) -> None:
    """$25 and $120 against a $50 rule - the fixture asserts both sides of it."""
    limit = config.tolerances.unmatched_charge_abs
    assert Decimal("25.00") <= limit
    assert Decimal("120.00") > limit

    small = [t for t in truths if t.variant is GeneratedVariant.FREIGHT_SMALL]
    large = [t for t in truths if t.variant is GeneratedVariant.FREIGHT_LARGE]
    assert small
    assert large

    for truth in small:
        assert ReasonCode.LINE_NOT_ON_PO not in truth.expected_reason_codes
    for truth in large:
        assert ReasonCode.LINE_NOT_ON_PO in truth.expected_reason_codes
        assert truth.expected_match is ExpectedMatch.EXCEPTION


def test_the_freight_amounts_straddle_the_limit(config: GuardrailConfig) -> None:
    """Stated separately so a fixture regenerated with new amounts is caught.

    The generator's constants are the other half of the pair; if either moves
    without the other, one of these two tests fails.
    """
    assert config.tolerances.unmatched_charge_abs >= FREIGHT_SMALL_AMOUNT
    assert config.tolerances.unmatched_charge_abs < FREIGHT_LARGE_AMOUNT


def test_every_expected_reason_code_is_in_the_closed_vocabulary(
    truths: list[GeneratedInvoiceTruth],
) -> None:
    for truth in truths:
        for code in truth.expected_reason_codes:
            assert code in set(ReasonCode)


def test_a_clean_invoice_against_a_delivered_order_expects_no_exception(
    truths: list[GeneratedInvoiceTruth],
) -> None:
    """Guards the three tests above: if everything were an exception they would pass."""
    clean = [
        t
        for t in truths
        if t.variant is GeneratedVariant.CLEAN and t.expected_match is ExpectedMatch.MATCHED
    ]
    assert clean, "no clean invoice expects a match; the fixture is not exercising the pass case"
    for truth in clean:
        assert truth.expected_reason_codes == []


def test_the_fixture_is_valid_json_on_disk() -> None:
    """Cheap, and it catches a half-written regeneration before anything else does."""
    root = get_settings().data_dir / "generated" / "invoices"
    paths = sorted(root.rglob("truth.json"))
    if not paths:  # pragma: no cover - absent in CI
        pytest.skip("no generated fixture")
    for path in paths:
        assert json.loads(path.read_text(encoding="utf-8"))
