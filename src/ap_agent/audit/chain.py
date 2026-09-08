"""The tamper-evident chain over audit events. STUB.

Each event stores the hash of the one before it, so altering or removing any
event invalidates every hash after it. That does not stop someone with database
access from rewriting history - it makes rewritten history *detectable*, which
is what "tamper-evident" means and all a single-database design can honestly
claim.

Design constraints the implementation must satisfy:

* **Canonical encoding.** The hash is taken over a deterministic serialisation
  of the event - sorted keys, fixed number formatting, explicit nulls. If two
  processes can serialise the same event differently, verification fails at
  random and nobody trusts the chain again.
* **The hash covers ``prev_event_hash``.** Otherwise events can be reordered
  freely within their chain.
* **Verification is a full walk.** ``verify_chain`` recomputes from genesis. It
  is a batch job, not something on the write path.
* **Periodic anchoring.** The chain head should be published somewhere the
  database administrator does not control - a daily digest to an append-only
  store. Without an external anchor, someone who can rewrite the table can
  rewrite the whole chain consistently.

Not implemented: the canonical encoder, ``compute_event_hash``, and
``verify_chain``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ap_agent.contracts.audit import GENESIS_HASH

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ap_agent.contracts.audit import AuditEvent

__all__ = ["GENESIS_HASH", "ChainVerification", "compute_event_hash", "verify_chain"]


def compute_event_hash(event: AuditEvent) -> str:
    """Return the SHA-256 of the canonical encoding of ``event``.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError


class ChainVerification:
    """Result of walking one invoice's chain. STUB.

    Will carry: whether the chain is intact, the ``step_seq`` of the first
    break, and the number of events verified.
    """

    def __init__(self) -> None:
        raise NotImplementedError


def verify_chain(events: Sequence[AuditEvent]) -> ChainVerification:
    """Recompute an invoice's chain from genesis and report the first break.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
