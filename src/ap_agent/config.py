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
    text_extraction_model: str = Field(
        default="claude-haiku-4-5",
        description="The second reading, from the PDF text layer. A different and cheaper model "
        "on purpose: two reads only disagree usefully if they can fail differently.",
    )
    extraction_prompt_version: str = Field(
        default="extract_v2",
        description="Selects prompts/<version>.md for the vision seat, and is recorded on every "
        "result and audit row. A prompt change is a new file and a new version, never an "
        "edit in place - an extraction from last month has to stay explainable under the "
        "prompt that produced it.",
    )
    text_extraction_prompt_version: str = Field(
        default="extract_text_v2",
        description="The same, for the text seat.",
    )
    classification_model: str = Field(
        default="claude-sonnet-5",
        description="Model id for the exception-explanation seat - the second and last seat a "
        "model occupies. It has no tools and is shown codes and numbers, never document text.",
    )
    classification_prompt_version: str = Field(
        default="classify_v1",
        description="Selects prompts/<version>.md for the explanation seat. Versioned for the "
        "same reason as the extraction prompts.",
    )
    model_pricing_path: Path = Field(
        default=REPO_ROOT / "config" / "model_pricing.yaml",
        description="Per-model token prices, versioned. What an audit row's cost_usd was "
        "computed from, so a cost can be re-explained after list prices change.",
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
    receipts_path: Path = Field(
        default=REPO_ROOT / "data" / "generated" / "receipts.json",
        description="Goods receipts, the third leg of the three-way match. Owned by ap-agent "
        "because QuickBooks Online has no goods-receipt entity at all. A setting "
        "rather than a literal so a run can be replayed against the receipts as "
        "they stood at the time.",
    )
    vendor_master_path: Path = Field(
        default=REPO_ROOT / "config" / "sandbox_vendor_master.yaml",
        description="The one definition of a vendor. Committed and versioned, unlike the "
        "corpus, because who a supplier is and what country they are in is a "
        "reviewable decision.",
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
