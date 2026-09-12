"""Fetch what was actually received against a purchase order.

Caller: code. The orchestrator calls this; no model can.
Side effects: LOCAL_READ.

Read-only. The third leg of a three-way match: what was ordered, what was
billed, and what actually turned up. It is the leg that catches a short
delivery, and the one an invoice cannot speak to - a vendor's claim about what
they shipped is the thing under test.

**This data is ours, and that is not a shortcut.** QuickBooks Online has no
goods-receipt entity at all. It models the purchase order and the bill and
nothing in between, so "what was received" has no home in the ERP and
``data/generated/receipts.json`` is where ap-agent keeps it. Discovering that
while writing the matcher would have been considerably worse than discovering it
while writing the seeder.

**An unknown purchase order returns an empty ``ReceiptSet``, never ``None``.**
Nothing arrived is a fact, and it is the fact that holds an invoice: an order
with no receipts against it must not pay, however clean the arithmetic. If this
returned ``None`` the most consequential case in the file would be shaped
exactly like a lookup that failed.

**Lines join on ``line_no``, which the file states.** They used to join on
*position*, which the file only implied - and which held exactly as long as
nobody reordered a purchase order. A reorder would have moved every quantity
onto the wrong line and the matcher would have reported variances on lines that
were fine, with nothing in the trail to say why. ``scripts/migrate_receipts.py``
wrote the numbers the positions implied; the seeder writes them now.

A line with no ``line_no`` falls back to its ``item`` name, because that is the
only other key both sides share. A file with neither is a file this cannot join,
and it says so rather than guessing.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import Field

from ap_agent.config import get_settings
from ap_agent.contracts.purchase_order import ReceiptLine, ReceiptSet
from ap_agent.errors import APAgentError
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

if TYPE_CHECKING:
    from datetime import date

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.LOCAL_READ,)
REQUIRES_IDEMPOTENCY_KEY = False

NOTHING_RECEIVED = "none"
"""The status the receipts file uses for an order where nothing turned up."""


class ReceiptsError(APAgentError):
    """The receipts file is missing or malformed.

    Distinct from "this purchase order has no receipts", which is an empty set
    and a normal answer. This is the file itself being unreadable, which is a
    configuration failure affecting every invoice.
    """


class GetReceiptsInput(ToolInput):
    """Input for :func:`get_receipts`."""

    po_number: str = Field(min_length=1, max_length=64)
    receipts_path: Path | None = Field(
        default=None,
        description="Overrides the configured path. For tests and for replaying a past run "
        "against the receipts as they stood then.",
    )


class GetReceiptsOutput(ToolOutput):
    """Output of :func:`get_receipts`."""

    receipts: ReceiptSet
    latency_ms: int = Field(ge=0)


def _load(path: Path) -> list[dict[str, Any]]:
    """Read every receipt document in the file."""
    if not path.is_file():
        msg = f"no receipts file at {path}. Run `just seed` first."
        raise ReceiptsError(msg)
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{path.name} is not valid JSON: {exc}"
        raise ReceiptsError(msg) from exc

    if not isinstance(loaded, dict):
        msg = f"{path.name} must be a JSON object with a `receipts` key"
        raise ReceiptsError(msg)

    records: Any = cast("dict[str, Any]", loaded).get("receipts")
    if not isinstance(records, list):
        msg = f"{path.name} declares no receipts"
        raise ReceiptsError(msg)
    return [cast("dict[str, Any]", r) for r in cast("list[Any]", records) if isinstance(r, dict)]


def to_receipt_set(po_number: str, documents: list[dict[str, Any]]) -> ReceiptSet:
    """Flatten every receipt document for one order into per-line quantities.

    Several deliveries against one order are several documents against the same
    lines, and the matcher's only question is how much arrived in total - so the
    quantities are summed by line here rather than in every caller.

    A line whose received quantity is zero is kept, not dropped. "Ordered, and
    nothing came" is the fact the whole leg exists to record, and a caller that
    could not tell it apart from "no such line" would be unable to raise the one
    reason code that matters most.

    Args:
        po_number: The order the set is for.
        documents: Receipt records, already filtered to that order.

    Returns:
        One ``ReceiptSet``, with lines in line-number order.
    """
    totals: dict[int, tuple[Decimal, date]] = {}
    for document in documents:
        received_on = cast("date", document.get("received_on"))
        raw_lines: Any = document.get("lines")
        lines = cast("list[Any]", raw_lines) if isinstance(raw_lines, list) else []
        by_name = _line_numbers_by_item(lines)
        for raw in lines:
            if not isinstance(raw, dict):
                continue
            line = cast("dict[str, Any]", raw)
            line_no = _line_no_of(po_number, line, by_name)
            quantity = Decimal(str(line.get("qty_received", "0")))
            previous = totals.get(line_no)
            if previous is None:
                totals[line_no] = (quantity, received_on)
            else:
                # Two deliveries against one line: add them, and date the line by
                # the later arrival, which is when it became complete.
                totals[line_no] = (previous[0] + quantity, max(previous[1], received_on))

    return ReceiptSet(
        po_number=po_number,
        lines=[
            ReceiptLine(line_no=line_no, qty_received=quantity, received_on=received_on)
            for line_no, (quantity, received_on) in sorted(totals.items())
        ],
    )


def _line_numbers_by_item(lines: list[Any]) -> dict[str, int]:
    """Fallback join key: item name to its position, for a file with no line_no.

    Only consulted when ``line_no`` is absent. A name that appears twice in one
    receipt cannot identify a line, so it is dropped from the mapping rather
    than resolving to whichever came last.
    """
    names: list[str] = [
        str(cast("dict[str, Any]", line).get("item", ""))
        for line in lines
        if isinstance(line, dict)
    ]
    return {
        name: index for index, name in enumerate(names, start=1) if name and names.count(name) == 1
    }


def _line_no_of(po_number: str, line: dict[str, Any], by_name: dict[str, int]) -> int:
    """The line's number, from the file or from its item name.

    Raises:
        ReceiptsError: The line carries neither, so nothing can join it to a
            purchase-order line. Loud rather than positional: a silent guess here
            puts a quantity on the wrong line and the matcher reports a variance
            on a line that was fine.
    """
    declared: Any = line.get("line_no")
    if isinstance(declared, int):
        return declared
    if isinstance(declared, str) and declared.isdigit():
        return int(declared)

    found = by_name.get(str(line.get("item", "")))
    if found is not None:
        return found

    msg = (
        f"receipt line for {po_number} has no line_no and no item name that identifies it. "
        f"Run `uv run python scripts/migrate_receipts.py`."
    )
    raise ReceiptsError(msg)


def get_receipts(payload: GetReceiptsInput) -> GetReceiptsOutput:
    """Fetch what was received against one purchase order.

    Args:
        payload: The PO number, and optionally a receipts file to read instead
            of the configured one.

    Returns:
        The receipt set - empty when the order is unknown or nothing arrived -
        and how long the read took.

    Raises:
        ReceiptsError: The receipts file is missing or malformed. That is a
            configuration failure, not a fact about this purchase order.
    """
    started = time.perf_counter()
    path = payload.receipts_path or get_settings().receipts_path

    documents = [
        document for document in _load(path) if str(document.get("po_number")) == payload.po_number
    ]
    receipts = to_receipt_set(payload.po_number, documents)
    return GetReceiptsOutput(
        receipts=receipts, latency_ms=int((time.perf_counter() - started) * 1000)
    )


__all__ = [
    "CALLER",
    "NOTHING_RECEIVED",
    "REQUIRES_IDEMPOTENCY_KEY",
    "SIDE_EFFECTS",
    "GetReceiptsInput",
    "GetReceiptsOutput",
    "ReceiptsError",
    "get_receipts",
    "to_receipt_set",
]
