"""Append-only writer for the audit trail. STUB.

The contract this must honour, stated here because the tests will assert it once
it exists:

* **Append only.** Insert, never update, never delete. The table has no ``UPDATE``
  grant for the application role; the migration is where that is enforced, not
  this module.
* **Same transaction as the state change.** Rule 4 of ``CLAUDE.md`` says every
  state transition writes an audit event. A separate transaction turns that rule
  into a race: the invoice moves, the process dies, and the trail has a hole
  exactly where something went wrong.
* **Sequential per invoice.** ``step_seq`` and ``prev_event_hash`` both require
  reading the current chain head under a lock. Concurrency is per-invoice, not
  per-event.
* **Redaction happens before the call.** ``tool_args_redacted`` arrives already
  redacted. The writer does not sanitise, because a writer that sanitises is a
  writer someone will eventually bypass.

Not implemented: the writer, the head lookup, and the transaction handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ap_agent.contracts.audit import AuditEvent


class AuditWriter(Protocol):
    """What the loop needs from an audit sink."""

    def append(self, event: AuditEvent) -> AuditEvent:
        """Persist ``event`` and return it with its chain fields filled in."""
        ...

    def head(self, invoice_id: str) -> AuditEvent | None:
        """Return the most recent event for ``invoice_id``, or None."""
        ...


class PostgresAuditWriter:
    """Append-only writer backed by the ``audit_events`` table. STUB."""

    def __init__(self) -> None:
        raise NotImplementedError

    def append(self, event: AuditEvent) -> AuditEvent:
        """Persist ``event`` in the caller's transaction.

        Raises:
            NotImplementedError: Written by hand in a later session.
        """
        raise NotImplementedError

    def head(self, invoice_id: str) -> AuditEvent | None:
        """Return the chain head for ``invoice_id``.

        Raises:
            NotImplementedError: Written by hand in a later session.
        """
        raise NotImplementedError
