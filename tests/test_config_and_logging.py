"""Settings, the documented environment, and log configuration.

The `.env.example` test is the one that earns its place: an undocumented setting
is a setting nobody sets, and the drift is invisible until someone deploys.
"""

from __future__ import annotations

import os

import structlog
from pydantic import SecretStr
from tests.conftest import SENTINEL_API_KEY

from ap_agent.config import REPO_ROOT, Settings, get_settings
from ap_agent.logging import configure_logging, get_logger


def _documented_env_keys() -> set[str]:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    return {
        line.split("=", 1)[0].strip().lower()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def test_env_example_documents_every_setting() -> None:
    documented = _documented_env_keys()
    missing = sorted(set(Settings.model_fields) - documented)
    assert not missing, f".env.example does not document: {missing}"


def test_env_example_documents_nothing_that_does_not_exist() -> None:
    stale = sorted(_documented_env_keys() - set(Settings.model_fields))
    assert not stale, f".env.example documents settings that do not exist: {stale}"


def test_the_shipped_defaults_need_no_credentials() -> None:
    """Ingestion, indexing, and the whole suite must run without a key.

    Asserted against the declared field defaults rather than a resolved
    ``Settings``. ``_env_file=None`` disables the dotenv *file*, but the deepeval
    pytest plugin loads `.env` into ``os.environ`` at collection time, so a
    resolved instance under pytest reflects whatever the developer has on disk.
    A test about shipped defaults must not read the ambient environment.
    """
    fields = Settings.model_fields
    assert fields["anthropic_api_key"].default.get_secret_value() == ""
    assert fields["qbo_environment"].default == "sandbox"


def test_secrets_do_not_render_in_a_repr() -> None:
    """A key that renders in a log line is a key in the log."""
    settings = Settings(anthropic_api_key=SecretStr("sk-ant-notreal"))
    assert "notreal" not in repr(settings)
    assert "notreal" not in str(settings.anthropic_api_key)
    assert "SecretStr" in repr(settings.anthropic_api_key)


def test_settings_are_cached() -> None:
    assert get_settings() is get_settings()


def test_the_guardrails_path_points_at_a_file_that_exists() -> None:
    assert Settings.model_fields["guardrails_config_path"].default.is_file()


def test_the_database_url_uses_psycopg3() -> None:
    """`postgresql://` would silently select psycopg2, which is not installed."""
    assert Settings.model_fields["database_url"].default.startswith("postgresql+psycopg://")


def test_configure_logging_is_idempotent() -> None:
    configure_logging(force=True)
    configure_logging()
    assert structlog.is_configured()


def test_get_logger_returns_a_bound_logger() -> None:
    logger = get_logger("test")
    assert hasattr(logger, "info")
    bound = logger.bind(invoice_id="inv-1")
    assert bound is not None


# --- the no-live-credentials guard ------------------------------------------


def test_the_suite_cannot_reach_the_api() -> None:
    """The guard in conftest, asserted rather than assumed.

    If this ever fails, a real key is live inside the test session and an
    unmocked call would bill a real account instead of erroring.
    """
    assert get_settings().anthropic_api_key.get_secret_value() == SENTINEL_API_KEY


def test_the_guard_survives_the_dotenv() -> None:
    """A fresh Settings must see the sentinel too, not the file on disk."""
    assert Settings().anthropic_api_key.get_secret_value() == SENTINEL_API_KEY


def test_no_fallback_credential_is_left_reachable() -> None:
    """Unsetting the key is not enough: the SDK falls through to these."""
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    assert "ANTHROPIC_PROFILE" not in os.environ
