"""Purchase orders and goods receipts, as returned by the ERP.

These are *system* records, not document readings. They are the authority a
``MatchResult`` is computed against, and nothing a model produces may modify
them. They live in ``contracts/`` because the rule stated in
``src/ap_agent/contracts/__init__.py`` holds for every schema in the system,
not only the ones a model touches.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money, Quantity, StrictModel, UnitPrice


class PurchaseOrderStatus(StrEnum):
    """Lifecycle of a PO in the ERP."""

    OPEN = "open"
    PARTIALLY_RECEIVED = "partially_received"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class PurchaseOrderLine(StrictModel):
    """One ordered line."""

    po_line_ref: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    quantity_ordered: Quantity
    unit: str = Field(min_length=1, max_length=16)
    unit_price: UnitPrice
    extended_price: Money
    gl_account: str | None = Field(default=None, max_length=32)
    cost_center: str | None = Field(default=None, max_length=32)


class PurchaseOrder(StrictModel):
    """A purchase order as held by the ERP."""

    po_number: str = Field(min_length=1, max_length=64)
    vendor_id: str = Field(min_length=1, max_length=64)
    status: PurchaseOrderStatus
    currency: CurrencyCode
    order_date: date
    buyer_user_id: str | None = Field(
        default=None, max_length=64, description="Routing target when the resolver is the buyer."
    )
    lines: list[PurchaseOrderLine] = Field(default_factory=list[PurchaseOrderLine], max_length=500)
    total: Money


class GoodsReceiptLine(StrictModel):
    """Quantity actually received against one PO line."""

    po_line_ref: str = Field(min_length=1, max_length=64)
    quantity_received: Decimal
    received_on: date


class GoodsReceipt(StrictModel):
    """A receiving record. The third leg of a three-way match."""

    receipt_id: str = Field(min_length=1, max_length=64)
    po_number: str = Field(min_length=1, max_length=64)
    received_on: date
    lines: list[GoodsReceiptLine] = Field(default_factory=list[GoodsReceiptLine], max_length=500)
