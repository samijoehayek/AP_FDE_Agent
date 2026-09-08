"""Open a durable approval request and notify the approver.

Caller: code. The orchestrator calls this; no model can.
Side effects: DB_WRITE, NOTIFICATION.

The human-in-the-loop boundary. The request is a row in the database, not a
callback or an in-memory future: the process can restart, the approver can
take four days, and the request survives both.

The approver is chosen by the approval matrix in the versioned guardrails
config - never by a model, and never by the invoice itself.

Not implemented: the approver resolution, the notification transport, the
reminder schedule, and the escalation path when nobody responds.

"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money
from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.DB_WRITE, SideEffect.NOTIFICATION)
REQUIRES_IDEMPOTENCY_KEY = True


class RequestApprovalInput(IdempotentToolInput):
    """Input for :func:`request_approval`."""

    invoice_id: str = Field(min_length=1, max_length=64)
    approver_user_id: str = Field(
        min_length=1,
        max_length=64,
        description="Resolved from the approval matrix by the caller. Recorded on the row.",
    )
    reason: str = Field(min_length=1, max_length=700, description="Display text for the queue.")
    amount: Money
    currency: CurrencyCode
    config_version: str = Field(min_length=1, max_length=32)
    due_by: datetime | None = None


class RequestApprovalOutput(ToolOutput):
    """Output of :func:`request_approval`."""

    approval_id: str = Field(min_length=1, max_length=64)
    already_existed: bool = False


def request_approval(payload: RequestApprovalInput) -> RequestApprovalOutput:
    """Open a durable approval request and notify the approver.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
