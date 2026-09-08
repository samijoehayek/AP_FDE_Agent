"""Propose GL account and cost-centre coding for review.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_READ, MODEL_CALL.

Proposes. For a PO-backed invoice the coding is inherited from the PO lines
and no model is involved. For a non-PO invoice a model may suggest a code
from the chart of accounts, and the suggestion is presented to the approver
as a default - never applied silently.

Not implemented: the chart-of-accounts loader, the vendor-history heuristic,
and the confidence threshold below which no default is offered at all.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.purchase_order import PurchaseOrder
from ap_agent.contracts.vendor import VendorRef
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.ERP_READ, SideEffect.MODEL_CALL)
REQUIRES_IDEMPOTENCY_KEY = False


class ProposedCoding(ToolOutput):
    """A suggested GL coding for one invoice line."""

    invoice_line_index: int = Field(ge=0)
    gl_account: str = Field(min_length=1, max_length=32)
    cost_center: str | None = Field(default=None, max_length=32)
    source: str = Field(
        description="'po_line', 'vendor_history', or 'model'. An approver should be able to "
        "see which, because the three deserve different scrutiny."
    )
    confidence: float = Field(ge=0.0, le=1.0)


class ProposeGlCodingInput(ToolInput):
    """Input for :func:`propose_gl_coding`."""

    extraction: InvoiceExtraction
    vendor: VendorRef | None = None
    purchase_orders: list[PurchaseOrder] = Field(default_factory=list[PurchaseOrder])
    model_id: str | None = Field(
        default=None, description="None for the PO-backed path, which is pure inheritance."
    )


class ProposeGlCodingOutput(ToolOutput):
    """Output of :func:`propose_gl_coding`."""

    line_codings: list[ProposedCoding] = Field(default_factory=list[ProposedCoding])
    requires_human_confirmation: bool = True


def propose_gl_coding(payload: ProposeGlCodingInput) -> ProposeGlCodingOutput:
    """Propose GL account and cost-centre coding for review.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
