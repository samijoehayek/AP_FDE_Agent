"""Purchase orders and goods receipts, as the systems of record hold them.

These are *system* records, not document readings. They are the authority a
``MatchResult`` is computed against, and nothing a model produces may modify
them. They live in ``contracts/`` because the rule stated in
``src/ap_agent/contracts/__init__.py`` holds for every schema in the system, not
only the ones a model touches.

Both shapes carry only what the matcher will compare. There are no account
references, no memo fields, no buyer routing and no header total - a contract
that carries what nothing reads invites code to start reading it, and every
extra field on a system record is one more thing to keep true. The PO total in
particular is deliberately absent: the matcher sums the lines it is comparing,
which cannot disagree with itself the way a stored header total can.

The two join on ``line_no``, which is the only key both sides have.
QuickBooks numbers purchase-order lines from 1 in creation order; the receipts
file has no line references at all and is positional against the same order. See
:class:`ReceiptSet` for what that assumption costs and how it is checked.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money, Quantity, StrictModel, UnitPrice


class PurchaseOrderStatus(StrEnum):
    """Lifecycle of a PO in the ERP.

    Wider than QuickBooks, which emits only ``Open`` and ``Closed``. The other
    two members are here because the concept is real in an AP function and a
    different ERP will supply them; nothing maps onto them today.
    """

    OPEN = "open"
    PARTIALLY_RECEIVED = "partially_received"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class PurchaseOrderLine(StrictModel):
    """One ordered line, as the ERP holds it."""

    line_no: int = Field(
        ge=1,
        description="1-based position in the order, from the ERP's own line numbering. "
        "The join key to a receipt line.",
    )
    item_ref: str | None = Field(
        default=None,
        max_length=64,
        description="The ERP's item identifier. None on a line booked to an account rather "
        "than a catalogue item.",
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="As the ERP holds it, which is not necessarily what the invoice printed. "
        "Optional because QuickBooks does not require it, and a line with no "
        "description is still a line that was ordered.",
    )
    qty_ordered: Quantity
    unit_price: UnitPrice
    extended: Money


class PurchaseOrder(StrictModel):
    """A purchase order as held by the ERP.

    ``vendor_erp_id`` is the identity check the pipeline makes before matching:
    an invoice quoting a purchase-order number belonging to a different supplier
    is a fraud signal, not a variance, and it must never reach a tolerance.
    """

    po_number: str = Field(
        min_length=1, max_length=64, description="The document number a vendor would print."
    )
    erp_id: str = Field(
        min_length=1,
        max_length=64,
        description="The ERP's own primary key, which is not the document number.",
    )
    vendor_erp_id: str = Field(min_length=1, max_length=64)
    vendor_name: str = Field(
        min_length=1,
        max_length=200,
        description="As the ERP holds it. Compared to the resolved vendor, never to the "
        "document - the document's claim is what is under suspicion.",
    )
    currency: CurrencyCode
    po_date: date
    status: PurchaseOrderStatus
    lines: list[PurchaseOrderLine] = Field(default_factory=list[PurchaseOrderLine], max_length=500)


class ReceiptLine(StrictModel):
    """What arrived against one purchase-order line."""

    line_no: int = Field(ge=1, description="Joins to PurchaseOrderLine.line_no.")
    qty_received: Quantity = Field(
        description="Zero is a real answer and the interesting one: it means the line was "
        "ordered and nothing came."
    )
    received_on: date


class ReceiptSet(StrictModel):
    """Everything received against one purchase order, flattened by line.

    The third leg of the three-way match, and the leg the ERP cannot hold -
    QuickBooks Online has no goods-receipt entity, so this data belongs to
    ap-agent and lives in ``data/generated/receipts.json``.

    **A purchase order with nothing received is an empty ``lines`` list, never
    ``None``.** "Nothing arrived" is a fact the matcher must act on, and it is
    the single most important fact in the set - an invoice for goods that have
    not been received is an exception however correct its arithmetic. Modelling
    it as missing data would make the most consequential case
    indistinguishable from a lookup that failed.

    Flattened deliberately. The receipts file records one document per order, but
    an order received in three deliveries is three documents against the same
    lines, and the matcher's only question is how much arrived in total. Keeping
    the receipt documents separate would push that summation into every caller.
    """

    po_number: str = Field(min_length=1, max_length=64)
    lines: list[ReceiptLine] = Field(default_factory=list[ReceiptLine], max_length=500)

    @property
    def is_empty(self) -> bool:
        """True when nothing at all was received against this order."""
        return not self.lines

    def quantity_for(self, line_no: int) -> Quantity | None:
        """Received quantity for one PO line, or None if the line has no receipt.

        None and zero are different answers and callers must keep them apart:
        zero means the line was receipted and nothing came, None means nothing
        was receipted against it at all.
        """
        for line in self.lines:
            if line.line_no == line_no:
                return line.qty_received
        return None
