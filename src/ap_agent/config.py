"""Runtime settings, loaded from the environment and ``.env``.

Secrets live here and nowhere else: no module reads ``os.environ`` directly.
``.env`` is git-ignored; ``.env.example`` documents the shape.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
"""Repository root, resolved from this file's location rather than the CWD."""


class Settings(BaseSettings):
    """Process-wide configuration.

    Attributes are populated from environment variables (case-insensitive),
    falling back to ``.env`` in the repository root.
    """

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Anthropic -------------------------------------------------------
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Anthropic API key. Empty is valid for offline work (tests, ingestion).",
    )
    anthropic_workspace_id: str = Field(
        default="",
        description="Required only when the API key is organisation-scoped rather than scoped "
        "to a workspace. Such a key carries no workspace of its own, so the API rejects the "
        "request unless the `anthropic-workspace-id` header names one. Empty means the key "
        "already resolves a workspace and the header is omitted.",
    )
    extraction_model: str = Field(
        default="claude-sonnet-5",
        description="Model id for the document-extraction seat. This model has no tools.",
    )
    reasoning_model: str = Field(
        default="claude-opus-5",
        description="Model id for the exception-explanation seat.",
    )

    # --- Storage ---------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://ap_agent_app:ap_agent_app@localhost:5432/ap_agent",
        description="What the application connects as. An ordinary role, so the REVOKE that "
        "makes audit_events append-only actually binds. The +psycopg driver is psycopg 3.",
    )
    database_migration_url: str = Field(
        default="postgresql+psycopg://ap_agent:ap_agent@localhost:5432/ap_agent",
        description="What Alembic connects as: the schema owner. Separate from database_url "
        "so the role that can ALTER a table is not the role that serves traffic.",
    )
    data_dir: Path = Field(
        default=REPO_ROOT / "data",
        description="Root of the git-ignored corpus tree. Never committed.",
    )

    # --- ERP (QuickBooks Online sandbox) ---------------------------------
    qbo_client_id: SecretStr = SecretStr("")
    qbo_client_secret: SecretStr = SecretStr("")
    qbo_refresh_token: SecretStr = Field(
        default=SecretStr(""),
        description="Long-lived OAuth token. Rotates on every refresh, so the value here is "
        "only a seed - the current one lives in data/.qbo_tokens.json.",
    )
    qbo_realm_id: str = ""
    qbo_environment: Literal["sandbox", "production"] = "sandbox"

    # --- Guardrails ------------------------------------------------------
    guardrails_config_path: Path = Field(
        default=REPO_ROOT / "config" / "guardrails.v1.yaml",
        description="Versioned tolerance + approval matrix. The version is part of the filename "
        "so an AuditEvent can name the exact ruleset that produced a decision.",
    )

    # --- Observability ---------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = Field(
        default=False,
        description="Render logs as JSON lines. Set true in CI and production.",
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that ``.env`` is read once. Call ``get_settings.cache_clear()``
    in a test that needs to re-read the environment.
    """
    return Settings()
