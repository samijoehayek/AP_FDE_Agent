"""Create an unpaid bill in the ERP.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_WRITE.

The only tool in this package that creates an accounting record, and it
creates an *unpaid* one. It records a liability; it does not move money, and
there is no tool here that does.

Preconditions the caller must have satisfied, and which this function
re-checks rather than trusts: the invoice is in ``APPROVED``, an approval row
exists with a human approver, and the approval matches this invoice's total
and vendor.

Idempotency: the key is derived from the invoice id, so a retry after a
timeout returns the bill already created rather than posting a second one.

Not implemented: the QBO client, the precondition re-check, and the mapping
from contracts to the ERP's bill payload.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money
from ap_agent.tools.base import IdempotentToolInput, SideEffect, ToolCaller, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.ERP_WRITE,)
REQUIRES_IDEMPOTENCY_KEY = True


class CreateBillInput(IdempotentToolInput):
    """Input for :func:`create_bill`."""

    invoice_id: str = Field(min_length=1, max_length=64)
    vendor_id: str = Field(min_length=1, max_length=64)
    approval_id: str = Field(
        min_length=1,
        max_length=64,
        description="The human approval authorising this. Re-verified here, not trusted.",
    )
    currency: CurrencyCode
    total: Money
    line_codings: list[dict[str, str]] = Field(default_factory=list[dict[str, str]])


class CreateBillOutput(ToolOutput):
    """Output of :func:`create_bill`."""

    erp_bill_id: str = Field(min_length=1, max_length=64)
    already_existed: bool = Field(
        description="True when the idempotency key matched a prior write. Not an error."
    )


def create_bill(payload: CreateBillInput) -> CreateBillOutput:
    """Create an unpaid bill in the ERP.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
