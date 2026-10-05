"""Content-addressed storage for everything an ``output_ref`` points at.

Responsible for one promise: **every ``output_ref`` on the trail resolves.**
Whatever was hashed into a reference is stored under that hash - the exact
bytes, not a re-serialisation - so a reader holding an audit row can fetch what
the row says came back, and prove it by hashing it again.

Two properties, both deliberate:

* **Stored before it is cited.** :func:`store_ref` writes the bytes and only
  then returns the reference, and references are made before the row that
  carries them is written. A process that dies between the two leaves an
  unreferenced payload, which is harmless, and never a reference to nothing.
* **Immutable.** The key is the sha256 of the content, so writing the same
  bytes twice is the same row, and nothing ever needs to change one. The
  application role has INSERT and SELECT on ``payloads`` and nothing else.

What it does not do: decide what is worth storing. Callers store what they
cite. Nothing is backfilled - trails written before this module existed carry
references that do not resolve, and say so honestly by failing to.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ap_agent.db.models import Payload

if TYPE_CHECKING:
    from pydantic import BaseModel
    from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "JSON",
    "TEXT",
    "VOLATILE_FIELDS",
    "InMemoryPayloadStore",
    "PayloadStore",
    "PostgresPayloadStore",
    "StoredPayload",
    "content_ref",
    "parse_ref",
    "serialise",
    "store_ref",
]

JSON: Final = "application/json"
TEXT: Final = "text/plain; charset=utf-8"

REF_PREFIX: Final = "sha256:"

VOLATILE_FIELDS: frozenset[str] = frozenset({"latency_ms", "input_tokens", "output_tokens"})
"""Excluded from a content address: they measure the call, not its answer.

``output_ref`` answers "what came back". How long it took and what it cost are
separate columns on the same row, and folding them into the hash would make the
same answer hash differently on every run - which destroys the one thing a
content address is for. They are excluded from the stored bytes for the same
reason: the payload is what was hashed, exactly.
"""


@dataclass(frozen=True)
class StoredPayload:
    """One payload, as stored."""

    sha256: str
    media_type: str
    content: bytes


class PayloadStore(Protocol):
    """Where payloads go. Content-addressed and write-once."""

    def put(self, content: bytes, media_type: str) -> str:
        """Store ``content`` and return its sha256 hex. Idempotent."""
        ...

    def get(self, sha256: str) -> StoredPayload | None:
        """Return the payload stored under ``sha256``, or None."""
        ...


def serialise(value: BaseModel | str) -> tuple[bytes, str]:
    """The exact bytes a reference is computed over, and their media type.

    JSON rather than a ``repr``, because a ``repr`` carries no stability
    guarantee and a reference that moved when pydantic changed its formatting
    would report tampering where none happened.
    """
    if isinstance(value, str):
        return value.encode("utf-8"), TEXT
    return value.model_dump_json(exclude=set(VOLATILE_FIELDS)).encode("utf-8"), JSON


def content_ref(value: BaseModel | str) -> str:
    """A content address for ``value``, for ``output_ref``. Computes; stores nothing.

    Human-readable notes do not go here. ``date=2024-03-09`` in an ``output_ref``
    is a label pretending to be an address; ``tool_result_summary`` exists for
    it.
    """
    content, _media_type = serialise(value)
    return REF_PREFIX + hashlib.sha256(content).hexdigest()


def store_ref(store: PayloadStore, value: BaseModel | str) -> str:
    """Store ``value`` and return its reference. Stored first, then cited."""
    content, media_type = serialise(value)
    return REF_PREFIX + store.put(content, media_type)


def parse_ref(ref: str) -> str:
    """Accept ``sha256:<hex>`` or bare hex; return the hex, lower-cased.

    Raises:
        ValueError: It is not a sha256.
    """
    digest = ref.strip().lower().removeprefix(REF_PREFIX)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):  # noqa: PLR2004 - sha256 hex
        msg = f"not a sha256 reference: {ref!r}"
        raise ValueError(msg)
    return digest


@dataclass
class InMemoryPayloadStore:
    """A store that lives as long as the process. For tests and offline runs."""

    payloads: dict[str, StoredPayload] = field(default_factory=dict[str, StoredPayload])

    def put(self, content: bytes, media_type: str) -> str:
        """Store ``content`` under its hash. A second put of the same bytes is a no-op."""
        digest = hashlib.sha256(content).hexdigest()
        self.payloads.setdefault(digest, StoredPayload(digest, media_type, content))
        return digest

    def get(self, sha256: str) -> StoredPayload | None:
        """Return the payload, or None."""
        return self.payloads.get(sha256)


@dataclass(frozen=True)
class PostgresPayloadStore:
    """``payloads`` in Postgres, as the application role.

    ``INSERT ... ON CONFLICT DO NOTHING``: the same bytes always have the same
    key, so a conflict means the payload is already there, and there is nothing
    to update - which is as well, since the role may not.
    """

    sessions: sessionmaker[Session]

    def put(self, content: bytes, media_type: str) -> str:
        """Store ``content`` under its hash, committed before this returns."""
        digest = hashlib.sha256(content).hexdigest()
        statement = (
            insert(Payload)
            .values(sha256=digest, media_type=media_type, content=content)
            .on_conflict_do_nothing(index_elements=[Payload.sha256])
        )
        with self.sessions() as session, session.begin():
            session.execute(statement)
        return digest

    def get(self, sha256: str) -> StoredPayload | None:
        """Return the payload, or None."""
        with self.sessions() as session:
            row = session.execute(
                select(Payload).where(Payload.sha256 == sha256)
            ).scalar_one_or_none()
        if row is None:
            return None
        return StoredPayload(row.sha256, row.media_type, row.content)
