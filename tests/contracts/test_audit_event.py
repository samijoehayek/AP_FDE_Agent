"""The audit record: actor discrimination, UTC timestamps, immutability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from ulid import ULID

from ap_agent.contracts.audit import (
    GENESIS_HASH,
    AuditEvent,
    HumanActor,
    ModelActor,
    RuleActor,
    SystemActor,
    ToolActor,
    utc_now,
)
from ap_agent.contracts.enums import AuditEventType
from ap_agent.states.machine import InvoiceState


def _event(**overrides: Any) -> AuditEvent:
    payload: dict[str, Any] = {
        "invoice_id": "01J8ZQ0000000000000000000A",
        "run_id": "run-1",
        "step_seq": 0,
        "ts_utc": utc_now(),
        "actor": SystemActor(),
        "event_type": AuditEventType.NOTE,
    }
    payload.update(overrides)
    return AuditEvent(**payload)


def test_event_id_defaults_to_a_ulid() -> None:
    assert isinstance(_event().event_id, ULID)


def test_event_ids_sort_by_creation_time() -> None:
    """ULIDs are lexicographically ordered, so the log sorts without a join."""
    first, second = _event(), _event()
    assert str(first.event_id) <= str(second.event_id)


def test_the_chain_starts_at_genesis() -> None:
    assert _event().prev_event_hash == GENESIS_HASH


def test_prev_event_hash_must_be_a_sha256_hex_digest() -> None:
    with pytest.raises(ValidationError):
        _event(prev_event_hash="not-a-hash")


def test_events_are_immutable() -> None:
    """An audit row that can be mutated in memory will eventually be mutated on disk."""
    event = _event()
    with pytest.raises(ValidationError):
        event.step_seq = 5  # type: ignore[misc]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _event(approved_by_the_invoice_itself=True)


# --- timestamps -------------------------------------------------------------


def test_a_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _event(ts_utc=datetime(2026, 9, 9, 12, 0, 0))  # noqa: DTZ001 - the point of the test


def test_a_non_utc_timestamp_is_rejected() -> None:
    """Mixed offsets make a trail unorderable later, and converting is cheap now."""
    berlin = timezone(timedelta(hours=2))
    with pytest.raises(ValidationError, match="must be timezone-aware and UTC"):
        _event(ts_utc=datetime(2026, 9, 9, 12, 0, 0, tzinfo=berlin))


def test_utc_now_is_aware_and_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert now.tzinfo is UTC


# --- actors -----------------------------------------------------------------


@pytest.mark.parametrize(
    "actor",
    [
        SystemActor(),
        RuleActor(rule_id="price_tolerance", config_version="v1"),
        ModelActor(model_id="claude-sonnet-5", prompt_version="extract-v3"),
        HumanActor(user_id="u-421"),
        ToolActor(name="create_bill"),
    ],
    ids=["system", "rule", "model", "human", "tool"],
)
def test_every_actor_kind_round_trips(actor: Any) -> None:
    event = _event(actor=actor)
    restored = AuditEvent.model_validate_json(event.model_dump_json())
    assert restored.actor == actor


def test_the_actor_union_is_discriminated_by_kind() -> None:
    event = AuditEvent.model_validate(
        {
            "invoice_id": "i-1",
            "run_id": "r-1",
            "step_seq": 1,
            "ts_utc": utc_now(),
            "actor": {"kind": "rule", "rule_id": "totals", "config_version": "v1"},
            "event_type": "rule_evaluation",
        }
    )
    assert isinstance(event.actor, RuleActor)
    assert event.actor.rule_id == "totals"


def test_an_unknown_actor_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _event(actor={"kind": "the_invoice", "rule_id": "x", "config_version": "v1"})


def test_a_rule_actor_must_name_its_config_version() -> None:
    """A decision that cannot name the ruleset that made it cannot be re-explained."""
    with pytest.raises(ValidationError):
        RuleActor(rule_id="price_tolerance")  # type: ignore[call-arg]


# --- state transitions ------------------------------------------------------


def test_a_state_transition_event_requires_both_endpoints() -> None:
    with pytest.raises(ValidationError, match="require both from_state and to_state"):
        _event(
            event_type=AuditEventType.STATE_TRANSITION,
            from_state=InvoiceState.RECEIVED,
        )


def test_a_complete_state_transition_event_validates() -> None:
    event = _event(
        event_type=AuditEventType.STATE_TRANSITION,
        from_state=InvoiceState.RECEIVED,
        to_state=InvoiceState.INGESTED,
    )
    assert event.to_state is InvoiceState.INGESTED


# --- cost accounting --------------------------------------------------------


def test_cost_is_decimal_and_non_negative() -> None:
    event = _event(cost_usd=Decimal("0.02"), input_tokens=1200, output_tokens=340)
    assert isinstance(event.cost_usd, Decimal)
    with pytest.raises(ValidationError):
        _event(cost_usd=Decimal("-0.01"))


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _event(confidence=1.5)
