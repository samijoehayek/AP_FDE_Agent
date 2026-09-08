"""Invoice state machine: the states, the events, and the table that joins them."""

from __future__ import annotations

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

__all__ = [
    "APPROVAL_GATE",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "IllegalTransition",
    "InvoiceEvent",
    "InvoiceState",
    "allowed_events",
    "is_terminal",
    "reachable_from",
    "to_mermaid",
    "transition",
]
