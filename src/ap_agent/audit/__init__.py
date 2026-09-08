"""The audit trail: the model (complete) and the writer (stub).

:class:`~ap_agent.contracts.audit.AuditEvent` itself is defined in
``ap_agent.contracts`` - contracts are the single source of truth for every
schema, and the audit record is not an exception to that. It is re-exported here
because this is where a reader will look for it.
"""

from __future__ import annotations

from ap_agent.audit.chain import ChainVerification, compute_event_hash, verify_chain
from ap_agent.audit.writer import AuditWriter, PostgresAuditWriter
from ap_agent.contracts.audit import (
    GENESIS_HASH,
    Actor,
    AuditEvent,
    HumanActor,
    ModelActor,
    RuleActor,
    SystemActor,
    ToolActor,
    utc_now,
)

__all__ = [
    "GENESIS_HASH",
    "Actor",
    "AuditEvent",
    "AuditWriter",
    "ChainVerification",
    "HumanActor",
    "ModelActor",
    "PostgresAuditWriter",
    "RuleActor",
    "SystemActor",
    "ToolActor",
    "compute_event_hash",
    "utc_now",
    "verify_chain",
]
