"""Match an invoice against its POs and receipts. Deterministic.

Caller: code. The orchestrator calls this; no model can.
Side effects: NONE.

Pure: same inputs, same ``MatchResult``, every time. This is the function
that decides whether money is owed, and it is code precisely so that its
output can be replayed, diffed, and unit-tested against the golden set.

Tolerances come from the versioned guardrails config and the version used is
recorded on the result, so a decision made last quarter can be re-explained
under the rules that were actually in force.

Not implemented: line assignment (invoice lines rarely arrive in PO order and
may be split or merged), the tolerance arithmetic, over-receipt and
under-receipt handling, and multi-PO invoices.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.contracts.matching import MatchResult
from ap_agent.contracts.purchase_order import PurchaseOrder, ReceiptSet
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.NONE,)
REQUIRES_IDEMPOTENCY_KEY = False


class ComputeMatchInput(ToolInput):
    """Input for :func:`compute_match`."""

    invoice_id: str = Field(min_length=1, max_length=64)
    extraction: InvoiceExtraction
    purchase_orders: list[PurchaseOrder] = Field(default_factory=list[PurchaseOrder])
    receipts: list[ReceiptSet] = Field(
        default_factory=list[ReceiptSet],
        description="One set per purchase order, parallel to `purchase_orders`. A PO with "
        "nothing received is an empty set, never absent - see ReceiptSet.",
    )
    config_version: str = Field(min_length=1, max_length=32)


class ComputeMatchOutput(ToolOutput):
    """Output of :func:`compute_match`."""

    result: MatchResult


def compute_match(payload: ComputeMatchInput) -> ComputeMatchOutput:
    """Match an invoice against its POs and receipts. Deterministic.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
