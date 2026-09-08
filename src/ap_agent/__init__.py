"""ap-agent: accounts-payable invoice processing.

An LLM occupies exactly two seats in this system - reading documents and
explaining exceptions in prose. Everything that decides money (matching,
tolerances, approval routing, posting) is deterministic code, and every state
change is written to an append-only, hash-chained audit trail.

Read ``CLAUDE.md`` at the repository root before extending this package: the
non-negotiable rules there are architectural, not stylistic.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
