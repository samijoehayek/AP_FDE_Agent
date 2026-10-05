"""Open a durable approval request for the people the matrix names.

Caller: code. The orchestrator calls this; no model can.
Side effects: DB_WRITE, NOTIFICATION. Writes an ``approvals`` row.

**Who must approve is decided before this is called**, by ``APPROVAL-MATRIX@v1``
(``guardrails/approval.py``): a count and a role, never a named person. This
records that requirement as a request that survives a deploy - approvals take
days - and it is completed only by people, through ``approval_decisions``.

**Idempotent.** The request's id is derived from the idempotency key, so a step
restarted after the row was written finds it (``already_existed``) instead of
opening a second request for the same invoice.

**Where it writes** is the store the loop bound for the run
(:data:`~ap_agent.persistence.CURRENT_APPROVAL_STORE`): a tool takes exactly
one argument, its input, and the run decides between Postgres and memory.
Called outside a run, it refuses.
"""

from __future__ import annotations

import hashlib

from pydantic import Field

from ap_agent.contracts.approval import ApprovalRequirement
from ap_agent.errors import APAgentError
from ap_agent.persistence import CURRENT_APPROVAL_STORE
from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.DB_WRITE, SideEffect.NOTIFICATION)
REQUIRES_IDEMPOTENCY_KEY = True


class RequestApprovalInput(IdempotentToolInput):
    """Input for :func:`request_approval`."""

    invoice_id: str = Field(min_length=1, max_length=64)
    requirement: ApprovalRequirement
    config_version: str = Field(min_length=1, max_length=32)


class RequestApprovalOutput(ToolOutput):
    """Output of :func:`request_approval`."""

    approval_id: str = Field(min_length=1, max_length=26)
    already_existed: bool = False
    required_approvers: int = Field(ge=1, le=4)
    approver_role: str = Field(min_length=1, max_length=64)


def approval_id_for(idempotency_key: str) -> str:
    """The request's id, derived from the key so a retry finds the same row."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:26]


def request_approval(payload: RequestApprovalInput) -> RequestApprovalOutput:
    """Open the request, or find the one this key already opened.

    Raises:
        APAgentError: Called outside a run, with no approval store bound.
    """
    try:
        store = CURRENT_APPROVAL_STORE.get()
    except LookupError as exc:
        msg = "request_approval was called outside a run; no approval store is bound"
        raise APAgentError(msg) from exc

    requirement = payload.requirement
    view, created = store.request(
        approval_id_for(payload.idempotency_key),
        payload.invoice_id,
        requirement,
        amount=requirement.amount,
        currency=requirement.currency,
        config_version=payload.config_version,
    )
    return RequestApprovalOutput(
        approval_id=view.approval_id,
        already_existed=not created,
        required_approvers=view.required_approvers,
        approver_role=view.approver_role,
    )
