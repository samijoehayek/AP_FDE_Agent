"""Flag a posted bill as cleared for the treasury run.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_WRITE.

Read the name carefully: this marks, it does not pay. It sets a flag on an
already-posted bill so that a separate treasury process - outside this
system, with its own controls and its own segregation of duties - can pick
it up.

This is the deliberate end of the pipeline's authority. The last mile of an
AP system is where fraud is monetised, and an agent has no business there.

Not implemented: the ERP client and the precondition re-check that the bill
is posted and approved.

"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.ERP_WRITE,)
REQUIRES_IDEMPOTENCY_KEY = True


class MarkReadyForPaymentInput(IdempotentToolInput):
    """Input for :func:`mark_ready_for_payment`."""

    invoice_id: str = Field(min_length=1, max_length=64)
    erp_bill_id: str = Field(min_length=1, max_length=64)
    scheduled_pay_date: date | None = None


class MarkReadyForPaymentOutput(ToolOutput):
    """Output of :func:`mark_ready_for_payment`."""

    marked: bool
    already_existed: bool = False


def mark_ready_for_payment(payload: MarkReadyForPaymentInput) -> MarkReadyForPaymentOutput:
    """Flag a posted bill as cleared for the treasury run.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
