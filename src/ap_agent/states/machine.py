"""The invoice lifecycle, as an explicit table.

Every legal move an invoice can make is a key in :data:`TRANSITIONS`. There is
no fallback branch, no "if the model thinks", and no way to reach a state by
setting a column. :func:`transition` is the only way to move, and it raises on
anything the table does not contain.

Why a table rather than methods on a state class: the table is the spec. It can
be diffed in review, rendered as the diagram in ``docs/ARCHITECTURE.md``, and
asserted against directly - the test that no path reaches ``POSTED`` without
passing through ``APPROVED`` is a graph search over this dict, so it stays true
as the table grows.

:func:`transition` is deliberately pure. It does not write the audit event that
rule 4 of ``CLAUDE.md`` requires; its caller does, inside the same database
transaction as the state write, so that a state change and its audit row commit
or fail together.
"""

from __future__ import annotations

from enum import StrEnum

from ap_agent.errors import IllegalTransition


class InvoiceState(StrEnum):
    """Every state an invoice can occupy."""

    RECEIVED = "RECEIVED"
    INGESTED = "INGESTED"
    NEEDS_HUMAN_EXTRACTION = "NEEDS_HUMAN_EXTRACTION"
    EXTRACTED = "EXTRACTED"
    VALIDATED = "VALIDATED"
    VENDOR_RESOLVED = "VENDOR_RESOLVED"
    NEW_VENDOR = "NEW_VENDOR"
    DUPLICATE_CHECKED = "DUPLICATE_CHECKED"
    ON_HOLD_DUPLICATE = "ON_HOLD_DUPLICATE"
    MATCHED = "MATCHED"
    EXCEPTION = "EXCEPTION"
    NON_PO = "NON_PO"
    CODED = "CODED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    POSTED = "POSTED"
    SCHEDULED = "SCHEDULED"
    PAID = "PAID"
    RECONCILED = "RECONCILED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class InvoiceEvent(StrEnum):
    """Every event that can drive a transition.

    Members are plain strings at runtime, so ``TRANSITIONS`` can be keyed and
    queried with either the enum or its value.
    """

    INGEST = "ingest"
    EXTRACT = "extract"
    EXTRACTION_FAILED = "extraction_failed"
    HUMAN_EXTRACTION_PROVIDED = "human_extraction_provided"
    VALIDATE = "validate"
    VALIDATION_FAILED = "validation_failed"
    RESOLVE_VENDOR = "resolve_vendor"
    VENDOR_NOT_FOUND = "vendor_not_found"
    VENDOR_ONBOARDED = "vendor_onboarded"
    CHECK_DUPLICATES = "check_duplicates"
    DUPLICATE_SUSPECTED = "duplicate_suspected"
    DUPLICATE_CLEARED = "duplicate_cleared"
    CONFIRM_DUPLICATE = "confirm_duplicate"
    MATCH = "match"
    MATCH_EXCEPTION = "match_exception"
    EXCEPTION_RESOLVED = "exception_resolved"
    NO_PO_REFERENCE = "no_po_reference"
    CODE = "code"
    REQUEST_APPROVAL = "request_approval"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    POST = "post"
    POST_FAILED = "post_failed"
    SCHEDULE_PAYMENT = "schedule_payment"
    PAYMENT_FAILED = "payment_failed"
    PAYMENT_CONFIRMED = "payment_confirmed"
    RECONCILE = "reconcile"
    CLOSE = "close"
    REJECT = "reject"
    CANCEL = "cancel"

    STUB_OK = "stub_ok"
    """A step whose tool is not written yet completed vacuously. TEMPORARY."""


TERMINAL_STATES: frozenset[InvoiceState] = frozenset(
    {InvoiceState.CLOSED, InvoiceState.REJECTED, InvoiceState.CANCELLED}
)
"""States with no outgoing edges. An invoice here is finished, one way or another."""

APPROVAL_GATE: InvoiceState = InvoiceState.APPROVED
"""The state that must precede POSTED. Asserted by a graph search in the tests."""

_S = InvoiceState
_E = InvoiceEvent

TRANSITIONS: dict[tuple[InvoiceState, str], InvoiceState] = {
    # --- intake -----------------------------------------------------------
    (_S.RECEIVED, _E.INGEST): _S.INGESTED,
    # --- extraction -------------------------------------------------------
    (_S.INGESTED, _E.EXTRACT): _S.EXTRACTED,
    (_S.INGESTED, _E.EXTRACTION_FAILED): _S.NEEDS_HUMAN_EXTRACTION,
    (_S.NEEDS_HUMAN_EXTRACTION, _E.HUMAN_EXTRACTION_PROVIDED): _S.EXTRACTED,
    # --- validation -------------------------------------------------------
    (_S.EXTRACTED, _E.VALIDATE): _S.VALIDATED,
    (_S.EXTRACTED, _E.VALIDATION_FAILED): _S.NEEDS_HUMAN_EXTRACTION,
    # --- vendor -----------------------------------------------------------
    (_S.VALIDATED, _E.RESOLVE_VENDOR): _S.VENDOR_RESOLVED,
    (_S.VALIDATED, _E.VENDOR_NOT_FOUND): _S.NEW_VENDOR,
    (_S.NEW_VENDOR, _E.VENDOR_ONBOARDED): _S.VENDOR_RESOLVED,
    (_S.NEW_VENDOR, _E.REJECT): _S.REJECTED,
    # --- duplicates -------------------------------------------------------
    (_S.VENDOR_RESOLVED, _E.CHECK_DUPLICATES): _S.DUPLICATE_CHECKED,
    (_S.VENDOR_RESOLVED, _E.DUPLICATE_SUSPECTED): _S.ON_HOLD_DUPLICATE,
    (_S.ON_HOLD_DUPLICATE, _E.DUPLICATE_CLEARED): _S.DUPLICATE_CHECKED,
    (_S.ON_HOLD_DUPLICATE, _E.CONFIRM_DUPLICATE): _S.REJECTED,
    # --- matching ---------------------------------------------------------
    (_S.DUPLICATE_CHECKED, _E.MATCH): _S.MATCHED,
    (_S.DUPLICATE_CHECKED, _E.MATCH_EXCEPTION): _S.EXCEPTION,
    (_S.DUPLICATE_CHECKED, _E.NO_PO_REFERENCE): _S.NON_PO,
    (_S.EXCEPTION, _E.EXCEPTION_RESOLVED): _S.MATCHED,
    (_S.EXCEPTION, _E.REJECT): _S.REJECTED,
    # --- coding -----------------------------------------------------------
    (_S.MATCHED, _E.CODE): _S.CODED,
    (_S.NON_PO, _E.CODE): _S.CODED,
    (_S.NON_PO, _E.REJECT): _S.REJECTED,
    # --- approval (the gate) ---------------------------------------------
    (_S.CODED, _E.REQUEST_APPROVAL): _S.PENDING_APPROVAL,
    (_S.PENDING_APPROVAL, _E.APPROVE): _S.APPROVED,
    (_S.PENDING_APPROVAL, _E.REQUEST_CHANGES): _S.EXCEPTION,
    (_S.PENDING_APPROVAL, _E.REJECT): _S.REJECTED,
    # --- posting and payment ---------------------------------------------
    (_S.APPROVED, _E.POST): _S.POSTED,
    (_S.APPROVED, _E.POST_FAILED): _S.EXCEPTION,
    (_S.POSTED, _E.SCHEDULE_PAYMENT): _S.SCHEDULED,
    (_S.SCHEDULED, _E.PAYMENT_CONFIRMED): _S.PAID,
    (_S.SCHEDULED, _E.PAYMENT_FAILED): _S.POSTED,
    (_S.PAID, _E.RECONCILE): _S.RECONCILED,
    (_S.RECONCILED, _E.CLOSE): _S.CLOSED,
}

_CANCELLABLE: tuple[InvoiceState, ...] = (
    _S.RECEIVED,
    _S.INGESTED,
    _S.NEEDS_HUMAN_EXTRACTION,
    _S.EXTRACTED,
    _S.VALIDATED,
    _S.VENDOR_RESOLVED,
    _S.NEW_VENDOR,
    _S.DUPLICATE_CHECKED,
    _S.ON_HOLD_DUPLICATE,
    _S.MATCHED,
    _S.EXCEPTION,
    _S.NON_PO,
    _S.CODED,
    _S.PENDING_APPROVAL,
)
"""Cancellation stops at the approval gate.

Once an invoice is APPROVED there is a liability in the ledger, and unwinding it
is an accounting action (a void or a credit) with its own trail - not a state
this pipeline may quietly rewrite.
"""

TRANSITIONS.update({(state, _E.CANCEL): _S.CANCELLED for state in _CANCELLABLE})


STUB_TRANSITIONS: dict[tuple[InvoiceState, str], InvoiceState] = {
    # TEMP STUB: duplicate detection is not implemented (find_duplicates).
    (_S.VENDOR_RESOLVED, _E.STUB_OK): _S.DUPLICATE_CHECKED,
    # TEMP STUB: GL coding is not implemented, and a non-PO invoice needs it
    # before anything else can happen to it. Without this edge every invoice
    # with no purchase-order reference - which is the whole Kaggle corpus -
    # stops dead at NON_PO and the rest of the pipeline is unexercisable.
    (_S.NON_PO, _E.STUB_OK): _S.CODED,
    # TEMP STUB: GL coding is not implemented (propose_gl_coding).
    (_S.MATCHED, _E.STUB_OK): _S.CODED,
    # TEMP STUB: approval routing is not implemented (request_approval).
    (_S.CODED, _E.STUB_OK): _S.PENDING_APPROVAL,
    # TEMP STUB: THE DANGEROUS ONE. This lets a machine approve an invoice with
    # no human anywhere near it, which is the single thing this system exists to
    # prevent. It is here only so the happy path can be exercised end to end
    # before request_approval is written. Delete it the moment approvals are
    # real, and never ship it.
    (_S.PENDING_APPROVAL, _E.STUB_OK): _S.APPROVED,
    # TEMP STUB: the ERP write is not implemented (create_bill).
    (_S.APPROVED, _E.STUB_OK): _S.POSTED,
    # TEMP STUB: payment scheduling is not implemented (mark_ready_for_payment).
    (_S.POSTED, _E.STUB_OK): _S.SCHEDULED,
    # TEMP STUB: payment confirmation comes from outside this system entirely.
    (_S.SCHEDULED, _E.STUB_OK): _S.PAID,
    # TEMP STUB: reconciliation is not implemented.
    (_S.PAID, _E.STUB_OK): _S.RECONCILED,
    # TEMP STUB: closing is not implemented.
    (_S.RECONCILED, _E.STUB_OK): _S.CLOSED,
}
"""Edges that exist only because the tools behind them do not.

Kept in their own table rather than mixed into ``TRANSITIONS`` so that "what is
real" and "what is scaffolding" can be told apart at a glance, and so a test can
enumerate them. ``tests/states/test_stub_transitions.py`` lists every one, which
means deleting a stub is a deliberate act with a failing test to confirm it -
not something that happens by accident while editing nearby lines.

DUPLICATE_CHECKED is no longer on this list. ``compute_match`` is real, so an
invoice leaves that state on MATCH or MATCH_EXCEPTION or not at all - and the
difference matters more than it sounds: the stub edge sent every invoice to
MATCHED regardless of what its numbers said, which is the shape of a system that
pays whatever it is sent.

Note what is deliberately absent: no stub edge leaves NEEDS_HUMAN_EXTRACTION,
ON_HOLD_DUPLICATE, EXCEPTION or NEW_VENDOR. Those states exist because a human
is required, and a stub that walked past them would be simulating the approval
this system is built to insist on. The loop asks for a stub step there, the
table refuses, and the run halts - which is the correct outcome.
"""

TRANSITIONS.update(STUB_TRANSITIONS)


def transition(state: InvoiceState, event: str) -> InvoiceState:
    """Return the state reached by applying ``event`` to ``state``.

    Args:
        state: The invoice's current state.
        event: An :class:`InvoiceEvent` member, or its string value.

    Returns:
        The new state.

    Raises:
        IllegalTransition: If ``(state, event)`` is not in :data:`TRANSITIONS`.
            This includes every event applied to a terminal state.
    """
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        msg = (
            f"no transition from {state.value} on event {event!r}; "
            f"legal events here: {sorted(str(event) for event in allowed_events(state))}"
        )
        raise IllegalTransition(msg) from None


def allowed_events(state: InvoiceState) -> set[str]:
    """Return every event that is legal from ``state``."""
    return {event for (source, event) in TRANSITIONS if source == state}


def is_terminal(state: InvoiceState) -> bool:
    """Return whether ``state`` has no outgoing transitions."""
    return state in TERMINAL_STATES


def reachable_from(
    start: InvoiceState,
    *,
    blocked: frozenset[InvoiceState] = frozenset(),
) -> set[InvoiceState]:
    """Return every state reachable from ``start``, never entering ``blocked``.

    Used by the tests to assert structural invariants - most importantly that
    with ``APPROVED`` blocked, ``POSTED`` becomes unreachable.

    Args:
        start: State to search from.
        blocked: States the search may not pass through.

    Returns:
        The set of reachable states, excluding ``start`` unless a cycle
        returns to it.
    """
    seen: set[InvoiceState] = set()
    frontier: list[InvoiceState] = [start]
    while frontier:
        current = frontier.pop()
        for (source, _event), target in TRANSITIONS.items():
            if source is not current or target in blocked or target in seen:
                continue
            seen.add(target)
            frontier.append(target)
    return seen


def to_mermaid() -> str:
    """Render :data:`TRANSITIONS` as a Mermaid ``stateDiagram-v2`` block.

    The diagram in ``docs/ARCHITECTURE.md`` is generated from this, so it cannot
    drift from the table it documents. Regenerate with ``just docs-diagram``.
    """
    lines = ["stateDiagram-v2", "    [*] --> RECEIVED"]
    for (source, event), target in sorted(
        TRANSITIONS.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        lines.append(f"    {source.value} --> {target.value}: {event}")
    lines.extend(f"    {state.value} --> [*]" for state in sorted(TERMINAL_STATES))
    return "\n".join(lines)
