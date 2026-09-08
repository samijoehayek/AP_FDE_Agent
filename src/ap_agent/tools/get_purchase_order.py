"""Fetch a purchase order from the ERP.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_READ.

Read-only. The PO is the authority a match is computed against, so this
function is the boundary at which a claim printed on an invoice becomes a
fact from the system of record.

Not implemented: the ERP client, the PO-number normalisation (vendors print
"PO-1234", "po1234", and "1234" for the same order), and the cache.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.purchase_order import PurchaseOrder
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.ERP_READ,)
REQUIRES_IDEMPOTENCY_KEY = False


class GetPurchaseOrderInput(ToolInput):
    """Input for :func:`get_purchase_order`."""

    po_number: str = Field(min_length=1, max_length=64)
    vendor_id: str | None = Field(
        default=None,
        description="When supplied, a PO belonging to a different vendor is not returned. "
        "A PO number alone is not proof of entitlement.",
    )


class GetPurchaseOrderOutput(ToolOutput):
    """Output of :func:`get_purchase_order`."""

    purchase_order: PurchaseOrder | None = None
    not_found_reason: str | None = Field(default=None, max_length=200)


def get_purchase_order(payload: GetPurchaseOrderInput) -> GetPurchaseOrderOutput:
    """Fetch a purchase order from the ERP.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
