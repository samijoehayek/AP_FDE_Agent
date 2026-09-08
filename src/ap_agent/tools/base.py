"""Conventions every tool module in this package follows.

A tool is a module, not a class. Each one declares, at module level:

``CALLER``
    Who is allowed to invoke it. Today that is :attr:`ToolCaller.CODE` for every
    tool in this package, and ``tests/tools/test_tool_contracts.py`` asserts it.
    Neither LLM seat in this system is given tools: the extraction model reads a
    document and returns a typed object, and the explanation model reads a
    finished ``MatchResult`` and returns prose. Widening this is an
    architectural change, not a refactor.

``SIDE_EFFECTS``
    What the tool touches outside the process. A reviewer should be able to
    answer "what can this call change?" from the module header alone.

``REQUIRES_IDEMPOTENCY_KEY``
    True for anything that writes to the ERP or sends a message. A retried
    ``create_bill`` must not create a second bill; the test suite asserts that
    every tool declaring an external write also declares a key.

Input and output models live beside the function in the same module so that the
signature and the schema cannot drift apart.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from ap_agent.contracts.common import StrictModel


class ToolCaller(StrEnum):
    """Who may invoke a tool."""

    CODE = "code"
    """Called by the orchestrator. Not exposed to any model."""

    MODEL = "model"
    """Exposed to an LLM as a callable tool. Nothing in this package uses it."""


class SideEffect(StrEnum):
    """What a tool touches beyond its own return value."""

    NONE = "none"
    """Pure. Same inputs, same outputs, no I/O."""

    LOCAL_READ = "local_read"
    """Reads the local filesystem."""

    DB_READ = "db_read"
    DB_WRITE = "db_write"

    ERP_READ = "erp_read"
    ERP_WRITE = "erp_write"
    """Creates or modifies a record in the accounting system. Idempotency required."""

    EXTERNAL_READ = "external_read"
    """Calls a third party over the network. Rate-limited and cached."""

    MODEL_CALL = "model_call"
    """Spends tokens against the Anthropic API."""

    NOTIFICATION = "notification"
    """Sends a message to a human. Visible to someone outside the system."""


class ToolInput(StrictModel):
    """Base for tool input models."""


class ToolOutput(StrictModel):
    """Base for tool output models."""


class IdempotentToolInput(ToolInput):
    """Base for the input of any tool that writes outside the process.

    The key is supplied by the caller and derived from the work being done -
    typically ``f"{invoice_id}:{operation}"`` - so that a retry after a timeout
    reuses it and the downstream system deduplicates rather than double-posting.
    """

    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        description="Stable across retries of the same logical operation.",
    )
