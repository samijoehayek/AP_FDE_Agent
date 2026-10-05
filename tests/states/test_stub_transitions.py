"""The scaffolding edges, listed by name.

Every stub transition exists only because the tool behind it does not. Listing
them here means deleting one is a deliberate act with a failing test to confirm
it, rather than something that happens while editing nearby lines - and it means
nobody has to read the loop to find out how much of the pipeline is real.
"""

from __future__ import annotations

from ap_agent.loop.runner import STUB_HOOKS
from ap_agent.states.machine import (
    STUB_TRANSITIONS,
    TRANSITIONS,
    InvoiceEvent,
    InvoiceState,
    reachable_from,
)

EXPECTED_STUB_EDGES: set[tuple[str, str]] = {
    # VALIDATED -> VENDOR_RESOLVED is gone: lookup_vendor is real, and the
    # invoice now travels that edge on resolve_vendor or not at all.
    ("VENDOR_RESOLVED", "DUPLICATE_CHECKED"),
    # DUPLICATE_CHECKED -> MATCHED is gone: compute_match is real, and an invoice
    # now leaves that state on what its numbers say or does not leave it at all.
    # GL coding is unwritten, and without this edge the entire Kaggle corpus -
    # which cites no purchase orders - stops dead at NON_PO.
    ("NON_PO", "CODED"),
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


def test_vendor_resolution_is_no_longer_scaffolding() -> None:
    """lookup_vendor is real, so an invoice may not reach VENDOR_RESOLVED on a stub.

    The edge it used to travel is gone. What replaces it is a real decision with
    two outcomes, and one of them - NEW_VENDOR - is a state no stub can leave.
    """
    assert (InvoiceState.VALIDATED, InvoiceEvent.STUB_OK.value) not in STUB_TRANSITIONS
    assert (InvoiceState.VALIDATED, InvoiceEvent.RESOLVE_VENDOR.value) in TRANSITIONS
    assert (InvoiceState.VALIDATED, InvoiceEvent.VENDOR_NOT_FOUND.value) in TRANSITIONS


def test_no_stub_walks_past_a_state_that_needs_a_human() -> None:
    """The states that exist because a person is required must stay stuck.

    A stub edge out of any of these would simulate the human decision the whole
    system is built to insist on.
    """
    human_required = {
        InvoiceState.NEEDS_HUMAN_EXTRACTION,
        InvoiceState.ON_HOLD_DUPLICATE,
        InvoiceState.PENDING_HUMAN,
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


EXPECTED_STUB_HOOKS: set[str] = set()
"""Behaviour a stub *step* performs, as opposed to an edge it travels.

Empty, and that is the point. A stub is supposed to do nothing, and this set
exists so a stub that starts doing something has to be written down.

It held one entry - the vendor-locale date resolution - for exactly as long as
``lookup_vendor`` was a stub. The country is now put on the record by the tool
that owns it, which is where it always belonged, and this went back to empty as
the comment said it should.
"""


def test_the_stub_hooks_are_exactly_these() -> None:
    """A stub edge is scaffolding; a stub *hook* is scaffolding that acts.

    Listed for the same reason as the edges: so that adding one is a deliberate
    act with a failing test to confirm it, and so nobody has to read the loop to
    find out what the scaffolding does.
    """
    assert STUB_HOOKS == EXPECTED_STUB_HOOKS


def test_no_stub_step_does_anything_at_all() -> None:
    """The state the scaffolding should always be in, stated directly."""
    assert not STUB_HOOKS


def test_the_approval_gate_still_holds_with_stubs_present() -> None:
    """The one invariant the scaffolding must not break.

    A stub may approve (temporarily, and loudly commented), but nothing may
    reach POSTED without passing through APPROVED.
    """
    reachable = reachable_from(InvoiceState.RECEIVED, blocked=frozenset({InvoiceState.APPROVED}))
    assert InvoiceState.POSTED not in reachable
