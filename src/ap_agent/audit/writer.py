"""Append-only writer for the audit trail, and the hash chain over it.

The contract this honours:

* **Append only.** Every write is one line appended to one file. Nothing is
  updated and nothing is deleted. The JSONL form is deliberate for this stage -
  a file that is only ever appended to is the simplest thing that can be
  audited, and it needs no database to be inspected during development.
* **Chained.** Each line records the SHA-256 of the previous line for the same
  invoice, so altering or removing any line invalidates every hash after it.
  That does not make history unchangeable - anyone who can write the file can
  rewrite the whole chain - it makes a change *detectable*, which is the honest
  claim for any single-store design. Making it tamper-*proof* needs an external
  anchor: publishing the head somewhere the writer does not control.
* **Hash over the line, not the object.** The chain covers exactly the bytes on
  disk. Hashing a re-serialisation would let a formatting change look like
  tampering, and a tamper look like a formatting change.

One file per invoice. The chain is per-invoice, so a single file keeps the whole
of one invoice's history in one place and makes concurrent work on different
invoices independent.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

from ap_agent.contracts.audit import GENESIS_HASH, AuditEvent

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["AuditWriter", "JsonlAuditWriter", "PostgresAuditWriter", "chain_hash"]


def chain_hash(line: str) -> str:
    """Return the SHA-256 of one serialised event line.

    Taken over the line as written, encoded UTF-8 and with the trailing newline
    stripped, so that reading the file back and hashing what is there reproduces
    exactly what the next line recorded.
    """
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()


class AuditWriter(Protocol):
    """What the loop needs from an audit sink."""

    def append(self, event: AuditEvent) -> AuditEvent:
        """Persist ``event`` and return it with its chain fields filled in."""
        ...

    def head(self, invoice_id: str) -> AuditEvent | None:
        """Return the most recent event for ``invoice_id``, or None."""
        ...


class JsonlAuditWriter:
    """One append-only JSONL file per invoice, hash-chained line to line."""

    def __init__(self, path_dir: Path) -> None:
        self._dir = path_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, invoice_id: str) -> Path:
        """Return the file holding ``invoice_id``'s history."""
        return self._dir / f"{invoice_id}.jsonl"

    def _lines(self, invoice_id: str) -> list[str]:
        path = self.path_for(invoice_id)
        if not path.is_file():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append(self, event: AuditEvent) -> AuditEvent:
        """Append ``event``, chaining it to whatever is already on disk.

        The caller's ``prev_event_hash`` is ignored and replaced. Letting a
        caller choose its own predecessor would make the chain decorative - the
        writer is the only thing that knows what is actually last.

        The contract's first link is :data:`GENESIS_HASH` (64 zeros) rather than
        an empty string, because ``prev_event_hash`` is a validated SHA-256 hex
        field and an empty string is not one.
        """
        invoice_id = event.invoice_id
        existing = self._lines(invoice_id)
        previous = chain_hash(existing[-1]) if existing else GENESIS_HASH

        chained = event.model_copy(update={"prev_event_hash": previous})
        line = chained.model_dump_json()

        with self.path_for(invoice_id).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return chained

    def head(self, invoice_id: str) -> AuditEvent | None:
        """Return the last event written for ``invoice_id``."""
        lines = self._lines(invoice_id)
        return AuditEvent.model_validate_json(lines[-1]) if lines else None

    def read(self, invoice_id: str) -> list[AuditEvent]:
        """Return the whole chain, oldest first."""
        return [AuditEvent.model_validate_json(line) for line in self._lines(invoice_id)]

    def verify(self, invoice_id: str) -> bool:
        """Walk the chain and report whether it is intact.

        Recomputes each line's hash and checks the next line recorded it. A
        modified, reordered, inserted or removed line breaks the link that
        follows it, so any of those returns False.

        A malformed line counts as broken. A line that cannot be parsed is not
        evidence of nothing having happened - it is evidence of something.
        """
        previous = GENESIS_HASH
        for line in self._lines(invoice_id):
            try:
                event = AuditEvent.model_validate_json(line)
            except ValueError:
                return False
            if event.prev_event_hash != previous:
                return False
            previous = chain_hash(line)
        return True

    def verify_detailed(self, invoice_id: str) -> tuple[bool, int | None]:
        """Return ``(intact, index_of_first_break)``.

        The index is what an operator needs: "the chain broke at line 4" points
        at the event to go and look at, where a bare False does not.
        """
        previous = GENESIS_HASH
        for index, line in enumerate(self._lines(invoice_id)):
            try:
                event = AuditEvent.model_validate_json(line)
            except ValueError:
                return False, index
            if event.prev_event_hash != previous:
                return False, index
            previous = chain_hash(line)
        return True, None


class PostgresAuditWriter:
    """Append-only writer backed by the ``audit_events`` table. STUB.

    The JSONL writer above is the development sink. This one is where the trail
    belongs in production, because the state write and the audit write have to
    commit in one transaction - a file cannot join the caller's transaction, so
    a crash between the two leaves a state change with no record of it.
    """

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
