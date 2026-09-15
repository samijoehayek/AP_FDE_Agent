"""Tolerances and the approval matrix, loaded from versioned YAML.

The numbers themselves live in ``config/guardrails.v<N>.yaml`` and their shape
lives in ``ap_agent.contracts.guardrails`` - contracts belong in one package and
this is not it. What is here is the loader, and nothing else.
"""

from __future__ import annotations

from ap_agent.guardrails.config import (
    GuardrailsError,
    load_guardrails,
    version_from_filename,
)

__all__ = ["GuardrailsError", "load_guardrails", "version_from_filename"]
