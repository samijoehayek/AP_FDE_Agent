"""Fetch a purchase order from the ERP.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_READ.

Read-only. The PO is the authority a match is computed against, so this function
is the boundary at which a claim printed on an invoice becomes a fact from the
system of record. Everything above this line is what a vendor said; everything
below it is what the buyer's own books say.

**The mapping is explicit and everything else is dropped.** QuickBooks returns
roughly forty keys on a purchase order - account references, tax detail, custom
fields, memos, a linked-transaction graph. Exactly the fields the matcher will
compare are carried across and the rest is discarded at this boundary, so that
"what QuickBooks calls a purchase order" cannot leak into "what this system does
about one". A field that arrives here and is never read is a field someone will
eventually start reading.

**Not found is ``None``, never an exception.** An invoice quoting a PO number
that does not exist is an ordinary business outcome with a routing decision
attached, and raising would make the loop handle it as a crash. Client errors -
a 401, a 500, an unreachable host - do propagate: those are not answers about
the purchase order, they are the absence of an answer, and treating them as "no
such PO" would let an outage look like a vendor mistake.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from pydantic import Field

from ap_agent.contracts.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from ap_agent.logging import get_logger
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

if TYPE_CHECKING:
    from datetime import date

    from ap_agent.integrations.qbo.client import QboClient

log = get_logger(__name__)

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.ERP_READ,)
REQUIRES_IDEMPOTENCY_KEY = False

QBO_ENTITY = "PurchaseOrder"

STATUS_BY_QBO_VALUE: dict[str, PurchaseOrderStatus] = {
    "open": PurchaseOrderStatus.OPEN,
    "closed": PurchaseOrderStatus.CLOSED,
}
"""QuickBooks emits only these two in ``POStatus``.

An unrecognised value maps to ``OPEN`` and is logged. That is the conservative
direction: ``OPEN`` is the state that lets the matcher keep looking at the
order, where guessing ``CLOSED`` would raise ``PO_CLOSED`` against an order
that is nothing of the kind and send a correct invoice to a person.
"""

ITEM_LINE_DETAIL = "ItemBasedExpenseLineDetail"
ACCOUNT_LINE_DETAIL = "AccountBasedExpenseLineDetail"
_LINE_DETAILS = (ITEM_LINE_DETAIL, ACCOUNT_LINE_DETAIL)


class GetPurchaseOrderInput(ToolInput):
    """Input for :func:`get_purchase_order`."""

    po_number: str = Field(
        min_length=1,
        max_length=64,
        description="The document number as printed on the invoice, used verbatim. "
        "Normalisation - vendors print 'PO-1234', 'po1234' and '1234' for the same "
        "order - is a separate problem and is not solved here.",
    )


class GetPurchaseOrderOutput(ToolOutput):
    """Output of :func:`get_purchase_order`."""

    purchase_order: PurchaseOrder | None = Field(
        default=None, description="None means the ERP has no order with that document number."
    )
    latency_ms: int = Field(ge=0)


def _rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the records from a QuickBooks query response.

    QuickBooks omits the entity key entirely when nothing matches rather than
    sending an empty list, so "no results" and "unexpected shape" look alike
    from outside. Both are no results here, which is exactly what the caller is
    prepared for.
    """
    envelope: dict[str, Any] = response.get("QueryResponse") or {}
    rows: Any = envelope.get(QBO_ENTITY)
    if not isinstance(rows, list):
        return []
    return [cast("dict[str, Any]", row) for row in cast("list[Any]", rows) if isinstance(row, dict)]


def _escape(value: str) -> str:
    """Escape a literal for a QuickBooks query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _decimal_text(value: object) -> str:
    """Render a QuickBooks number as text for ``Decimal`` to parse.

    Intuit sends amounts as JSON numbers, so they have already been through a
    float by the time this sees them. ``repr`` of that float is the shortest
    string that round-trips to the same value, which is the closest thing to the
    original the wire format allows - and going through ``str(float)`` keeps the
    loss in one place rather than scattering ``Decimal(float)`` across the file,
    which would silently produce 17 digits of noise.
    """
    return repr(value) if isinstance(value, float) else str(value)


def _status_of(record: dict[str, Any]) -> PurchaseOrderStatus:
    """Map ``POStatus`` onto the contract's enum, conservatively."""
    raw: Any = record.get("POStatus")
    if raw is None:
        return PurchaseOrderStatus.OPEN
    found = STATUS_BY_QBO_VALUE.get(str(raw).strip().casefold())
    if found is None:
        log.warning("qbo_unknown_po_status", status=str(raw), doc_number=record.get("DocNumber"))
        return PurchaseOrderStatus.OPEN
    return found


def _line_of(raw: dict[str, Any], fallback_no: int) -> PurchaseOrderLine | None:
    """Map one QuickBooks line, or None if it is not a billable line.

    QuickBooks puts subtotals, discounts and group headers in the same ``Line``
    array as the things that were actually ordered, distinguished only by
    ``DetailType``. A matcher fed a subtotal row as if it were a line would
    report a quantity variance on a row nobody ordered.
    """
    detail_type = str(raw.get("DetailType") or "")
    if detail_type not in _LINE_DETAILS:
        return None

    detail: dict[str, Any] = raw.get(detail_type) or {}
    item: dict[str, Any] = detail.get("ItemRef") or {}
    item_value: Any = item.get("value")

    quantity = _decimal_text(detail.get("Qty", 0))
    unit_price = _decimal_text(detail.get("UnitPrice", 0))
    amount = _decimal_text(raw.get("Amount", 0))
    line_num: Any = raw.get("LineNum")
    description: Any = raw.get("Description")

    return PurchaseOrderLine(
        line_no=int(line_num) if line_num is not None else fallback_no,
        item_ref=str(item_value) if item_value is not None else None,
        # QuickBooks does not require a description, and a line without one is
        # still a line that was ordered.
        description=str(description) if description else None,
        qty_ordered=quantity,  # pyright: ignore[reportArgumentType] - validated to Decimal
        unit_price=unit_price,  # pyright: ignore[reportArgumentType]
        extended=amount,  # pyright: ignore[reportArgumentType]
    )


def to_purchase_order(record: dict[str, Any]) -> PurchaseOrder:
    """Map one QuickBooks ``PurchaseOrder`` record onto the contract.

    Separate from the fetch so the mapping can be tested against a captured
    response without a client, and so the one place that knows Intuit's field
    names is one function long.

    Args:
        record: A single record from a ``PurchaseOrder`` query response.

    Returns:
        The contract's view: the document number, the ERP's own id, the vendor's
        id and name, currency, date, status and the billable lines.
    """
    vendor: dict[str, Any] = record.get("VendorRef") or {}
    currency: dict[str, Any] = record.get("CurrencyRef") or {}
    raw_lines: Any = record.get("Line")
    lines_in = cast("list[Any]", raw_lines) if isinstance(raw_lines, list) else []

    lines: list[PurchaseOrderLine] = []
    for index, raw in enumerate(lines_in, start=1):
        if not isinstance(raw, dict):
            continue
        mapped = _line_of(cast("dict[str, Any]", raw), index)
        if mapped is not None:
            lines.append(mapped)

    txn_date: Any = record.get("TxnDate")
    return PurchaseOrder(
        po_number=str(record.get("DocNumber") or ""),
        erp_id=str(record.get("Id") or ""),
        vendor_erp_id=str(vendor.get("value") or ""),
        vendor_name=str(vendor.get("name") or ""),
        currency=str(currency.get("value") or "USD"),
        po_date=cast("date", txn_date),  # validated by pydantic from the ISO string
        status=_status_of(record),
        lines=lines,
    )


def get_purchase_order(payload: GetPurchaseOrderInput) -> GetPurchaseOrderOutput:
    """Fetch a purchase order from the ERP by its document number.

    Args:
        payload: The PO number as printed on the invoice.

    Returns:
        The order, or ``purchase_order=None`` when the ERP has no such document
        number, and how long the call took.

    Raises:
        QboError: The request failed. Propagated deliberately: an outage is not
            an answer about the purchase order, and treating it as "not found"
            would route an invoice to an exception for a reason that is not
            about the invoice.
    """
    started = time.perf_counter()
    client = _client()

    statement = (
        f"SELECT * FROM {QBO_ENTITY} WHERE DocNumber = '{_escape(payload.po_number)}' MAXRESULTS 1"  # noqa: S608 - not SQL, and the value is a PO number the caller supplied
    )
    rows = _rows(client.query(statement))
    elapsed = int((time.perf_counter() - started) * 1000)

    if not rows:
        return GetPurchaseOrderOutput(purchase_order=None, latency_ms=elapsed)
    return GetPurchaseOrderOutput(purchase_order=to_purchase_order(rows[0]), latency_ms=elapsed)


def _client() -> QboClient:
    """Build the client at call time.

    Imported and constructed here rather than at module import so that importing
    this module - which the tool-registry tests do for every tool - never needs
    QuickBooks credentials.
    """
    from ap_agent.integrations.qbo.client import QboClient  # noqa: PLC0415

    return QboClient()


__all__ = [
    "CALLER",
    "QBO_ENTITY",
    "REQUIRES_IDEMPOTENCY_KEY",
    "SIDE_EFFECTS",
    "STATUS_BY_QBO_VALUE",
    "GetPurchaseOrderInput",
    "GetPurchaseOrderOutput",
    "get_purchase_order",
    "to_purchase_order",
]
