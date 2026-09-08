"""The shipped guardrails file must match the shape the loader will validate.

The loader is a stub, but the YAML is real and is what a reviewer reads first.
Validating it against the models here means the file and the schema cannot drift
while the loader is unwritten.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from ap_agent.config import REPO_ROOT
from ap_agent.contracts.common import CURRENCY_ALLOWLIST
from ap_agent.guardrails.config import GuardrailsConfig

CONFIG_PATH = REPO_ROOT / "config" / "guardrails.v1.yaml"


@pytest.fixture(scope="module")
def config() -> GuardrailsConfig:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return GuardrailsConfig.model_validate(raw)


def test_the_shipped_config_validates(config: GuardrailsConfig) -> None:
    assert config.version == "v1"


def test_the_version_matches_the_filename(config: GuardrailsConfig) -> None:
    """An AuditEvent names a config version; the version must locate the file."""
    assert CONFIG_PATH.name == f"guardrails.{config.version}.yaml"


def test_the_base_currency_is_in_the_allowlist(config: GuardrailsConfig) -> None:
    assert config.base_currency in CURRENCY_ALLOWLIST


def test_straight_through_processing_is_off_by_default(config: GuardrailsConfig) -> None:
    """Turning it on is a reviewed change with a golden-set number attached."""
    assert config.auto_approve_max_amount == Decimal("0.00")


def test_the_hard_stops_are_on(config: GuardrailsConfig) -> None:
    assert config.block_on_bank_details_mismatch
    assert config.new_vendor_requires_external_verification


def test_the_approval_matrix_ends_unbounded(config: GuardrailsConfig) -> None:
    """An invoice above every band must still have an approver."""
    assert config.approval_matrix
    assert config.approval_matrix[-1].max_amount is None


def test_the_approval_matrix_bands_ascend(config: GuardrailsConfig) -> None:
    bounded = [rule.max_amount for rule in config.approval_matrix if rule.max_amount is not None]
    assert bounded == sorted(bounded)


def test_only_one_versioned_config_is_shipped() -> None:
    """Old versions are kept once they exist; today there is exactly one."""
    files = sorted(Path(REPO_ROOT / "config").glob("guardrails.v*.yaml"))
    assert [f.name for f in files] == ["guardrails.v1.yaml"]
