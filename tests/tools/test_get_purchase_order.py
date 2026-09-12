"""Turning a QuickBooks purchase order into the contract the matcher compares.

The mapping is the whole risk here. Everything else in this file is a
consequence of one question: which of Intuit's forty-odd keys is this system
allowed to believe, and what happens to the rest.

No network. The client's ``query`` is replaced by a double returning
``qbo_purchase_order_response.json``, which is a **verbatim capture** of what the
sandbox returned for ``SELECT * FROM PurchaseOrder WHERE DocNumber =
'AP-SEED-001'`` on 2026-09-12. Nothing was stripped; a purchase order carries no
credentials.

The seeded order happens to have two well-formed lines, so the two cases
QuickBooks *can* send and this mapping must survive - a line with no
``Description``, and a ``SubTotalLineDetail`` row sitting in the same array as
the real lines - are built here by editing the captured record. Synthesising
them in the fixture would have made the fixture a guess again.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ap_agent.contracts.purchase_order import PurchaseOrderStatus
from ap_agent.integrations.qbo.client import QboError
from ap_agent.tools.get_purchase_order import (
    GetPurchaseOrderInput,
    get_purchase_order,
    to_purchase_order,
)

RESPONSE_PATH = Path(__file__).parent / "qbo_purchase_order_response.json"

PO_MODULE = sys.modules["ap_agent.tools.get_purchase_order"]
"""The module, taken from sys.modules rather than by attribute lookup.

``ap_agent.tools.__init__`` re-exports the *function* ``get_purchase_order``,
which rebinds the package attribute of that name - so
``import ap_agent.tools.get_purchase_order as m`` hands back the function and
patching an attribute on it fails with a confusing AttributeError. sys.modules
holds the real module.
"""


def _response() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RESPONSE_PATH.read_text(encoding="utf-8"))
    return loaded


def _record() -> dict[str, Any]:
    records: list[dict[str, Any]] = _response()["QueryResponse"]["PurchaseOrder"]
    return records[0]


class FakeQbo:
    """An in-memory QuickBooks that answers one query and remembers it."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload if payload is not None else _response()
        self.queries: list[str] = []

    def query(self, statement: str) -> dict[str, Any]:
        self.queries.append(statement)
        return self.payload


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeQbo:
    """Install the double in place of the client the tool builds at call time."""
    fake = FakeQbo()
    monkeypatch.setattr(PO_MODULE, "_client", lambda: fake)
    return fake


# --- the mapping ------------------------------------------------------------


def test_the_header_maps_onto_the_contract() -> None:
    order = to_purchase_order(_record())

    assert order.po_number == "AP-SEED-001"
    assert order.erp_id == "145"
    assert order.vendor_erp_id == "58"
    assert order.vendor_name == "TechVision Distributors Pvt Ltd"
    assert order.currency == "USD"
    assert order.po_date.isoformat() == "2026-07-27"
    assert order.status is PurchaseOrderStatus.OPEN


def test_the_document_number_and_the_erp_id_are_not_the_same_thing() -> None:
    """A vendor prints the first and has never seen the second."""
    order = to_purchase_order(_record())
    assert order.po_number != order.erp_id


def test_money_arrives_as_decimal_not_float() -> None:
    """Intuit sends JSON numbers. A float here fails a tolerance check later."""
    order = to_purchase_order(_record())
    first = order.lines[0]
    assert isinstance(first.unit_price, Decimal)
    assert isinstance(first.extended, Decimal)
    assert first.unit_price == Decimal("21400.00")
    assert first.extended == Decimal("64200.00")
    assert first.qty_ordered == Decimal(3)


def test_lines_carry_their_own_numbering() -> None:
    """line_no is the only key a receipt line can join on."""
    order = to_purchase_order(_record())
    assert [line.line_no for line in order.lines] == [1, 2]


def test_a_line_with_no_description_is_still_a_line() -> None:
    """QuickBooks does not require one, and something was still ordered.

    The seeded order has descriptions on both lines, so one is removed here -
    the mapping must not lose a line over a field nothing joins on.
    """
    record = _record()
    del record["Line"][1]["Description"]

    order = to_purchase_order(record)

    second = next(line for line in order.lines if line.line_no == 2)
    assert second.description is None
    assert second.item_ref == "20"
    assert second.qty_ordered == Decimal(11)


def test_a_subtotal_row_is_not_a_line_anyone_ordered() -> None:
    """QuickBooks puts subtotals in the same array as the real lines.

    A matcher handed one would report a quantity variance against a row nobody
    ordered, which is a hold on a correct invoice. The seeded order has no
    subtotal row, so one is added here in the shape QuickBooks sends.
    """
    record = _record()
    record["Line"].append(
        {"Amount": 272100.0, "DetailType": "SubTotalLineDetail", "SubTotalLineDetail": {}}
    )

    assert len(to_purchase_order(record).lines) == 2


def test_everything_else_in_the_response_is_dropped() -> None:
    """The boundary. What QuickBooks calls a PO must not become what we do about one."""
    order = to_purchase_order(_record())
    carried = set(order.model_dump())
    assert carried == {
        "po_number",
        "erp_id",
        "vendor_erp_id",
        "vendor_name",
        "currency",
        "po_date",
        "status",
        "lines",
    }
    assert "SyncToken" not in carried
    assert "APAccountRef" not in carried
    assert "TotalAmt" not in carried


def test_a_closed_order_maps_to_closed() -> None:
    record = _record() | {"POStatus": "Closed"}
    assert to_purchase_order(record).status is PurchaseOrderStatus.CLOSED


def test_an_unrecognised_status_falls_back_to_open() -> None:
    """The conservative direction: OPEN lets the matcher keep looking.

    Guessing CLOSED would raise PO_CLOSED against an order that is nothing of
    the kind, and send a correct invoice to a person.
    """
    record = _record() | {"POStatus": "Something Intuit Added Later"}
    assert to_purchase_order(record).status is PurchaseOrderStatus.OPEN


# --- fetching ---------------------------------------------------------------


@pytest.mark.usefixtures("fake_client")
def test_a_found_order_comes_back_mapped() -> None:
    found = get_purchase_order(GetPurchaseOrderInput(po_number="AP-SEED-001"))

    assert found.purchase_order is not None
    assert found.purchase_order.po_number == "AP-SEED-001"
    assert found.latency_ms >= 0


def test_the_query_filters_on_the_document_number(fake_client: FakeQbo) -> None:
    get_purchase_order(GetPurchaseOrderInput(po_number="AP-SEED-001"))

    (statement,) = fake_client.queries
    assert "FROM PurchaseOrder" in statement
    assert "DocNumber = 'AP-SEED-001'" in statement


def test_a_quote_in_a_po_number_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing read off a document should reach a query unescaped, ever."""
    fake = FakeQbo({"QueryResponse": {}})
    monkeypatch.setattr(PO_MODULE, "_client", lambda: fake)

    get_purchase_order(GetPurchaseOrderInput(po_number="PO-'DROP"))

    assert "PO-\\'DROP" in fake.queries[0]


def test_an_unknown_order_is_none_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A business outcome with a routing decision, not a crash."""
    fake = FakeQbo({"QueryResponse": {}})
    monkeypatch.setattr(PO_MODULE, "_client", lambda: fake)

    found = get_purchase_order(GetPurchaseOrderInput(po_number="AP-SEED-999"))

    assert found.purchase_order is None


def test_an_empty_result_list_is_also_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """QuickBooks omits the key rather than sending an empty list; handle both."""
    fake = FakeQbo({"QueryResponse": {"PurchaseOrder": []}})
    monkeypatch.setattr(PO_MODULE, "_client", lambda: fake)

    assert get_purchase_order(GetPurchaseOrderInput(po_number="X")).purchase_order is None


def test_a_client_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """An outage is not an answer about the purchase order.

    Swallowing it would let a 500 look like a vendor quoting a PO that does not
    exist, and route a correct invoice to an exception for a reason that has
    nothing to do with the invoice.
    """

    class Broken:
        def query(self, statement: str) -> dict[str, Any]:
            del statement
            msg = "QuickBooks GET query failed: HTTP 500"
            raise QboError(msg, status_code=500)

    monkeypatch.setattr(PO_MODULE, "_client", Broken)

    with pytest.raises(QboError):
        get_purchase_order(GetPurchaseOrderInput(po_number="AP-SEED-001"))
