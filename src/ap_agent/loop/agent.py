"""The orchestrator. STUB - written by hand in a later session.

What will live here, so that a reader knows what is missing rather than
guessing:

The loop advances one invoice through the state machine. At each state it
selects the next step from a fixed policy, calls the tool for that step, writes
an ``AuditEvent``, and applies ``transition()`` - in that order, with the audit
row and the state write in one database transaction so they cannot diverge.

Three properties it must have, all of which are cheaper to design in now than to
retrofit:

* **Resumable.** State lives in Postgres, not in the process. Killing the worker
  mid-invoice and restarting must resume from the last committed state, and an
  approval that takes four days must not hold a process open.
* **Bounded.** A step budget per run and a wall-clock deadline. An agent that
  can loop forever will eventually loop forever on the one invoice that costs
  the most tokens.
* **Not in charge of money.** The loop chooses which deterministic function to
  call next. It does not decide tolerances, approvers, or whether to pay - those
  come from the guardrails config and from humans.

The LLM is not the loop. It occupies two seats inside it (extraction and
exception explanation) and returns typed objects; the control flow between those
calls is ordinary code with a state machine, which is what makes it reviewable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ap_agent.states.machine import InvoiceState


def run_invoice(invoice_id: str, *, run_id: str, max_steps: int = 50) -> InvoiceState:
    """Advance one invoice as far as it can go without a human.

    Args:
        invoice_id: The invoice to advance.
        run_id: Identifier for this pass, recorded on every audit event.
        max_steps: Hard cap on steps before the run yields.

    Returns:
        The state the invoice came to rest in.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
