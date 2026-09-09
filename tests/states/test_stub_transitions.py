"""The scaffolding edges, listed by name.

Every stub transition exists only because the tool behind it does not. Listing
them here means deleting one is a deliberate act with a failing test to confirm
it, rather than something that happens while editing nearby lines - and it means
nobody has to read the loop to find out how much of the pipeline is real.
"""

from __future__ import annotations

from ap_agent.states.machine import (
    STUB_TRANSITIONS,
    TRANSITIONS,
    InvoiceEvent,
    InvoiceState,
    reachable_from,
)

EXPECTED_STUB_EDGES: set[tuple[str, str]] = {
    ("VALIDATED", "VENDOR_RESOLVED"),
    ("VENDOR_RESOLVED", "DUPLICATE_CHECKED"),
    ("DUPLICATE_CHECKED", "MATCHED"),
    ("MATCHED", "CODED"),
    ("CODED", "PENDING_APPROVAL"),
    ("PENDING_APPROVAL", "APPROVED"),
    ("APPROVED", "POSTED"),
    ("POSTED", "SCHEDULED"),
    ("SCHEDULED", "PAID"),
    ("PAID", "RECONCILED"),
    ("RECONCILED", "CLOSED"),
}


def test_the_stub_edges_are_exactly_these() -> None:
    """Adding or removing one must be deliberate enough to edit this list."""
    actual = {(src.value, dst.value) for (src, _event), dst in STUB_TRANSITIONS.items()}
    assert actual == EXPECTED_STUB_EDGES


def test_every_stub_edge_uses_the_stub_event() -> None:
    """A stub must be identifiable in the trail, not disguised as a real event."""
    assert all(event == InvoiceEvent.STUB_OK.value for (_src, event) in STUB_TRANSITIONS)


def test_no_stub_walks_past_a_state_that_needs_a_human() -> None:
    """The states that exist because a person is required must stay stuck.

    A stub edge out of any of these would simulate the human decision the whole
    system is built to insist on.
    """
    human_required = {
        InvoiceState.NEEDS_HUMAN_EXTRACTION,
        InvoiceState.ON_HOLD_DUPLICATE,
        InvoiceState.EXCEPTION,
        InvoiceState.NEW_VENDOR,
    }
    escapes = {src.value for (src, _event) in STUB_TRANSITIONS if src in human_required}
    assert not escapes, f"stub edges leave human-gated states: {sorted(escapes)}"


def test_the_stub_edges_are_merged_into_the_real_table() -> None:
    """They must be reachable through transition(), not a parallel mechanism."""
    assert all(key in TRANSITIONS for key in STUB_TRANSITIONS)


def test_no_stub_reaches_anywhere_the_real_table_cannot() -> None:
    """The scaffolding adds event names, never destinations.

    Every stub edge mirrors a real edge between the same two states - the tool
    behind it is missing, not the transition. So a stub can never move an
    invoice somewhere the designed pipeline does not already allow, and removing
    them all changes which *events* are accepted, not which states are reachable.
    """
    real_pairs = {
        (src, dst) for (src, event), dst in TRANSITIONS.items() if event != InvoiceEvent.STUB_OK
    }
    stub_pairs = {(src, dst) for (src, _event), dst in STUB_TRANSITIONS.items()}
    assert stub_pairs <= real_pairs, sorted((s.value, d.value) for s, d in stub_pairs - real_pairs)


def test_the_approval_gate_still_holds_with_stubs_present() -> None:
    """The one invariant the scaffolding must not break.

    A stub may approve (temporarily, and loudly commented), but nothing may
    reach POSTED without passing through APPROVED.
    """
    reachable = reachable_from(InvoiceState.RECEIVED, blocked=frozenset({InvoiceState.APPROVED}))
    assert InvoiceState.POSTED not in reachable
