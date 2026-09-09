"""The append-only audit record.

One row per thing that happened, in order, per invoice. ``prev_event_hash``
chains each event to the one before it so that a deletion or an edit anywhere in
an invoice's history breaks the chain from that point forward. The chain
algorithm itself is deliberately not implemented in this scaffold - see
``ap_agent.audit.chain``.

Design notes worth defending in review:

* ``actor`` is a discriminated union rather than a string. "Who did this" is the
  first question asked of an AP trail, and the answer must carry the details
  that make it checkable: which rule at which config version, which model at
  which prompt version, which human.
* ``step_seq`` is monotonic within ``(invoice_id, run_id)``. It, not the
  timestamp, defines order - clocks are not trustworthy and two events can share
  a millisecond.
* ``tool_args_redacted`` is redacted at construction, not at display. An audit
  row that has to be filtered before a human can read it is a leak waiting for
  the one query that forgets to filter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator
from ulid import ULID

from ap_agent.contracts.common import Confidence, CostUsd, Sha256Hex, StrictModel
from ap_agent.contracts.enums import ActorKind, AuditEventType
from ap_agent.states.machine import InvoiceState

GENESIS_HASH = "0" * 64
"""``prev_event_hash`` of the first event in an invoice's chain."""


class SystemActor(StrictModel):
    """The runtime itself: scheduling, retries, timeouts."""

    kind: Literal[ActorKind.SYSTEM] = ActorKind.SYSTEM


class RuleActor(StrictModel):
    """A deterministic rule. Every money decision has one of these as its actor."""

    kind: Literal[ActorKind.RULE] = ActorKind.RULE
    rule_id: str = Field(min_length=1, max_length=128)
    config_version: str = Field(
        min_length=1,
        max_length=32,
        description="Version of the guardrails config the rule was evaluated under.",
    )


class ModelActor(StrictModel):
    """An LLM call. Present only for extraction and exception explanation."""

    kind: Literal[ActorKind.MODEL] = ActorKind.MODEL
    model_id: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=32)


class HumanActor(StrictModel):
    """A person. The only actor that may approve."""

    kind: Literal[ActorKind.HUMAN] = ActorKind.HUMAN
    user_id: str = Field(min_length=1, max_length=128)


class ToolActor(StrictModel):
    """A tool invocation, attributed to the tool rather than to its caller."""

    kind: Literal[ActorKind.TOOL] = ActorKind.TOOL
    name: str = Field(min_length=1, max_length=128)


Actor = Annotated[
    SystemActor | RuleActor | ModelActor | HumanActor | ToolActor,
    Field(discriminator="kind"),
]
"""Tagged union of everything that can cause an audited event."""


class AuditEvent(StrictModel):
    """One immutable entry in an invoice's history.

    Instances are frozen. The writer appends; nothing updates.
    """

    event_id: ULID = Field(
        default_factory=ULID,
        description="ULID: lexicographically sortable by creation time, so the log sorts "
        "correctly without a join and ids do not leak a sequence.",
    )
    invoice_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(
        min_length=1,
        max_length=64,
        description="One pass of the agent loop. An invoice may have several runs; the chain "
        "spans all of them.",
    )
    step_seq: int = Field(
        ge=0, description="Monotonic within (invoice_id, run_id). Defines order, not the clock."
    )
    ts_utc: AwareDatetime

    actor: Actor
    event_type: AuditEventType

    from_state: InvoiceState | None = None
    to_state: InvoiceState | None = None

    input_ref: str | None = Field(
        default=None,
        max_length=512,
        description="Content-addressed pointer to the full input (payloads are not inlined).",
    )
    output_ref: str | None = Field(default=None, max_length=512)

    tool_name: str | None = Field(default=None, max_length=128)
    tool_args_redacted: dict[str, str] | None = Field(
        default=None,
        description="Arguments with secrets and PII already replaced. Redaction happens before "
        "construction, never at render time.",
    )
    tool_result_summary: str | None = Field(default=None, max_length=1000)

    model_id: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=32)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: CostUsd | None = Field(
        default=None,
        ge=0,
        description="Six places, not two: one extraction costs about $0.027, which Money "
        "would have rejected outright.",
    )
    latency_ms: int | None = Field(default=None, ge=0)
    trace_id: str | None = Field(
        default=None, max_length=128, description="Correlates this event with an external trace."
    )

    decision: str | None = Field(
        default=None, max_length=64, description="The outcome, e.g. 'approved', 'held', 'routed'."
    )
    decision_basis: str | None = Field(
        default=None,
        max_length=1000,
        description="Why. For a rule actor this is the rule expression and the values it saw.",
    )
    confidence: Confidence | None = None

    error_class: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)
    retry_count: int = Field(default=0, ge=0)

    prev_event_hash: Sha256Hex = Field(
        default=GENESIS_HASH,
        description="SHA-256 of the canonical encoding of the previous event in this invoice's "
        "chain, or GENESIS_HASH for the first.",
    )

    @model_validator(mode="after")
    def _timestamp_is_utc(self) -> Self:
        """Reject non-UTC timestamps outright.

        A trail that mixes offsets cannot be ordered or audited later, and the
        conversion is always cheaper to do at the edge than in a query.
        """
        offset = self.ts_utc.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            msg = "ts_utc must be timezone-aware and UTC"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _state_transition_names_both_states(self) -> Self:
        """A STATE_TRANSITION event without both endpoints is unreadable later."""
        if self.event_type is AuditEventType.STATE_TRANSITION and (
            self.from_state is None or self.to_state is None
        ):
            msg = "state_transition events require both from_state and to_state"
            raise ValueError(msg)
        return self


def utc_now() -> datetime:
    """Return an aware UTC timestamp suitable for :attr:`AuditEvent.ts_utc`."""
    return datetime.now(UTC)
