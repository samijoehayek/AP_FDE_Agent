"""The transition table is the specification. These tests assert it as one.

The structural tests matter more than the per-edge ones: a table this size will
grow, and "no path reaches POSTED without APPROVED" has to keep holding after
edges nobody remembers adding.
"""

from __future__ import annotations

import pytest

from ap_agent.errors import IllegalTransition
from ap_agent.states.machine import (
    APPROVAL_GATE,
    TERMINAL_STATES,
    TRANSITIONS,
    InvoiceEvent,
    InvoiceState,
    allowed_events,
    is_terminal,
    reachable_from,
    to_mermaid,
    transition,
)

TABLE_ENTRIES: list[tuple[InvoiceState, str, InvoiceState]] = sorted(
    ((source, event, target) for (source, event), target in TRANSITIONS.items()),
    key=lambda entry: (entry[0].value, entry[1]),
)
"""Every declared edge, flattened, so the table itself parametrises its own test."""

POST_APPROVAL_STATES = frozenset(
    {
        InvoiceState.POSTED,
        InvoiceState.SCHEDULED,
        InvoiceState.PAID,
        InvoiceState.RECONCILED,
        InvoiceState.CLOSED,
    }
)


@pytest.mark.parametrize(("state", "event", "expected"), TABLE_ENTRIES)
def test_every_table_entry_is_reachable_through_transition(
    state: InvoiceState, event: str, expected: InvoiceState
) -> None:
    """Full coverage of the table: every declared edge is walked."""
    assert transition(state, event) is expected


def test_every_event_key_is_a_known_event() -> None:
    known = {member.value for member in InvoiceEvent}
    unknown = {event for (_state, event) in TRANSITIONS if event not in known}
    assert not unknown, f"table uses undeclared events: {sorted(unknown)}"


def test_every_declared_event_is_used() -> None:
    """An event with no edge is dead vocabulary and misleads a reader."""
    used = {event for (_state, event) in TRANSITIONS}
    unused = {member.value for member in InvoiceEvent} - used
    assert not unused, f"declared but unused events: {sorted(unused)}"


def test_transition_accepts_plain_strings() -> None:
    """Events arrive from a queue as strings, so the table must be keyed by value."""
    assert transition(InvoiceState.RECEIVED, "ingest") is InvoiceState.INGESTED


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_have_no_outgoing_edges(state: InvoiceState) -> None:
    assert allowed_events(state) == set()
    assert is_terminal(state)


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
@pytest.mark.parametrize("event", sorted(member.value for member in InvoiceEvent))
def test_no_event_escapes_a_terminal_state(state: InvoiceState, event: str) -> None:
    with pytest.raises(IllegalTransition):
        transition(state, event)


def test_illegal_transition_names_the_legal_ones() -> None:
    """The error is read by whoever is debugging a stuck invoice at 2am."""
    with pytest.raises(IllegalTransition, match="ingest"):
        transition(InvoiceState.RECEIVED, "approve")


def test_cannot_approve_from_anywhere_but_pending_approval() -> None:
    sources = {state for (state, event) in TRANSITIONS if event == InvoiceEvent.APPROVE.value}
    assert sources == {InvoiceState.PENDING_APPROVAL}


def test_posted_is_only_ever_reached_from_approved() -> None:
    sources = {
        state for (state, _e), target in TRANSITIONS.items() if target is InvoiceState.POSTED
    }
    assert InvoiceState.APPROVED in sources
    assert sources <= {InvoiceState.APPROVED, InvoiceState.SCHEDULED}


def test_no_path_reaches_posted_without_passing_through_approved() -> None:
    """The load-bearing invariant of the whole machine.

    Searched as a graph rather than asserted edge by edge, so it survives edges
    added later by someone who has not read this file.
    """
    reachable = reachable_from(InvoiceState.RECEIVED, blocked=frozenset({APPROVAL_GATE}))
    assert InvoiceState.POSTED not in reachable


@pytest.mark.parametrize("state", sorted(POST_APPROVAL_STATES))
def test_no_post_approval_state_is_reachable_without_approval(state: InvoiceState) -> None:
    """Not just POSTED: nothing downstream of it either."""
    reachable = reachable_from(InvoiceState.RECEIVED, blocked=frozenset({APPROVAL_GATE}))
    assert state not in reachable


def test_every_state_is_reachable_from_received() -> None:
    """An unreachable state is either a bug or dead vocabulary."""
    reachable = reachable_from(InvoiceState.RECEIVED) | {InvoiceState.RECEIVED}
    assert set(InvoiceState) == reachable


def test_a_terminal_state_is_reachable_from_every_non_terminal_state() -> None:
    """No invoice can get stuck somewhere it can never leave."""
    for state in InvoiceState:
        if is_terminal(state):
            continue
        assert reachable_from(state) & TERMINAL_STATES, f"{state} cannot terminate"


def test_cancel_is_not_available_after_approval() -> None:
    """Once a liability exists, unwinding it is an accounting action with its own trail."""
    for state in (
        InvoiceState.APPROVED,
        InvoiceState.POSTED,
        InvoiceState.SCHEDULED,
        InvoiceState.PAID,
    ):
        assert InvoiceEvent.CANCEL.value not in allowed_events(state)


def test_happy_path_walks_end_to_end() -> None:
    path = [
        InvoiceEvent.INGEST,
        InvoiceEvent.EXTRACT,
        InvoiceEvent.VALIDATE,
        InvoiceEvent.RESOLVE_VENDOR,
        InvoiceEvent.CHECK_DUPLICATES,
        InvoiceEvent.MATCH,
        InvoiceEvent.CODE,
        InvoiceEvent.REQUEST_APPROVAL,
        InvoiceEvent.APPROVE,
        InvoiceEvent.POST,
        InvoiceEvent.SCHEDULE_PAYMENT,
        InvoiceEvent.PAYMENT_CONFIRMED,
        InvoiceEvent.RECONCILE,
        InvoiceEvent.CLOSE,
    ]
    state = InvoiceState.RECEIVED
    for event in path:
        state = transition(state, event)
    assert state is InvoiceState.CLOSED


def test_exception_loop_returns_to_the_approval_gate() -> None:
    state = InvoiceState.PENDING_APPROVAL
    state = transition(state, InvoiceEvent.REQUEST_CHANGES)
    assert state is InvoiceState.EXCEPTION
    state = transition(state, InvoiceEvent.EXCEPTION_RESOLVED)
    state = transition(state, InvoiceEvent.CODE)
    state = transition(state, InvoiceEvent.REQUEST_APPROVAL)
    assert state is InvoiceState.PENDING_APPROVAL


def test_mermaid_renders_every_state_and_edge() -> None:
    diagram = to_mermaid()
    assert diagram.startswith("stateDiagram-v2")
    for state in InvoiceState:
        assert state.value in diagram
    assert diagram.count("-->") >= len(TRANSITIONS)
