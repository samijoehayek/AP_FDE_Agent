"""Settings, the documented environment, and log configuration.

The `.env.example` test is the one that earns its place: an undocumented setting
is a setting nobody sets, and the drift is invisible until someone deploys.
"""

from __future__ import annotations

import structlog

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


def test_settings_work_with_no_environment_at_all() -> None:
    """Ingestion, indexing, and the whole suite must run without a key."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.anthropic_api_key.get_secret_value() == ""
    assert settings.qbo_environment == "sandbox"


def test_secrets_do_not_render_in_a_repr() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert "get_secret_value" not in repr(settings.anthropic_api_key)
    assert "SecretStr" in repr(settings.anthropic_api_key)


def test_settings_are_cached() -> None:
    assert get_settings() is get_settings()


def test_the_guardrails_path_points_at_a_file_that_exists() -> None:
    assert Settings(_env_file=None).guardrails_config_path.is_file()  # type: ignore[call-arg]


def test_the_database_url_uses_psycopg3() -> None:
    """`postgresql://` would silently select psycopg2, which is not installed."""
    assert Settings(_env_file=None).database_url.startswith(  # type: ignore[call-arg]
        "postgresql+psycopg://"
    )


def test_configure_logging_is_idempotent() -> None:
    configure_logging(force=True)
    configure_logging()
    assert structlog.is_configured()


def test_get_logger_returns_a_bound_logger() -> None:
    logger = get_logger("test")
    assert hasattr(logger, "info")
    bound = logger.bind(invoice_id="inv-1")
    assert bound is not None
