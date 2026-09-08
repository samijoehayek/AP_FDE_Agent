"""Fetch goods receipts for a purchase order.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_READ.

Read-only. The third leg of a three-way match: what was ordered, what was
billed, and what was actually received.

Not implemented: the ERP client and the handling of receipts that post after
the invoice arrives (which is a timing exception, not a mismatch).

"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from ap_agent.contracts.purchase_order import GoodsReceipt
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.ERP_READ,)
REQUIRES_IDEMPOTENCY_KEY = False


class GetReceiptsInput(ToolInput):
    """Input for :func:`get_receipts`."""

    po_number: str = Field(min_length=1, max_length=64)
    as_of: date | None = Field(
        default=None, description="Receipts posted after this date are excluded."
    )


class GetReceiptsOutput(ToolOutput):
    """Output of :func:`get_receipts`."""

    receipts: list[GoodsReceipt] = Field(default_factory=list[GoodsReceipt])


def get_receipts(payload: GetReceiptsInput) -> GetReceiptsOutput:
    """Fetch goods receipts for a purchase order.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
