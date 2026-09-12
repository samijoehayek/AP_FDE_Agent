"""What arrived against a purchase order, read from the file that owns it.

The distinction under test throughout: **nothing arrived** and **nobody asked**
are different answers, and this tool must never let them look alike. An empty
set is a fact that holds an invoice; a None would be a lookup that failed.

No network and nothing under ``data/``: every case writes its own receipts file
into tmp_path.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ap_agent.config import get_settings
from ap_agent.tools.get_receipts import (
    GetReceiptsInput,
    ReceiptsError,
    get_receipts,
)


def _receipts_file(tmp_path: Path, receipts: list[dict[str, Any]]) -> Path:
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps({"receipts": receipts}), encoding="utf-8")
    return path


def _document(po: str, status: str, lines: list[tuple[str, str]]) -> dict[str, Any]:
    """One receipt document, in the shape scripts/seed_sandbox.py writes."""
    return {
        "receipt_id": f"GR-{po}",
        "po_number": po,
        "status": status,
        "received_on": "2026-08-17",
        "lines": [
            {"item": item, "qty_ordered": ordered, "qty_received": received}
            for item, (ordered, received) in zip(
                [f"Item {n}" for n in range(1, len(lines) + 1)], lines, strict=True
            )
        ],
    }


FULL = _document("AP-FULL", "full", [("3", "3"), ("11", "11")])
PARTIAL = _document("AP-PARTIAL", "partial", [("12", "11"), ("11", "8")])
NONE_RECEIVED = _document("AP-NONE", "none", [("9", "0"), ("11", "0"), ("10", "0")])


@pytest.fixture
def receipts_path(tmp_path: Path) -> Path:
    return _receipts_file(tmp_path, [FULL, PARTIAL, NONE_RECEIVED])


def _fetch(po: str, path: Path) -> Any:
    return get_receipts(GetReceiptsInput(po_number=po, receipts_path=path)).receipts


# --- the three kinds of receipt ---------------------------------------------


def test_a_fully_received_order(receipts_path: Path) -> None:
    found = _fetch("AP-FULL", receipts_path)

    assert found.po_number == "AP-FULL"
    assert [line.line_no for line in found.lines] == [1, 2]
    assert [line.qty_received for line in found.lines] == [Decimal(3), Decimal(11)]
    assert found.is_empty is False


def test_a_partially_received_order_reports_what_came(receipts_path: Path) -> None:
    """Short of what was ordered, and the shortfall is the matcher's to judge."""
    found = _fetch("AP-PARTIAL", receipts_path)

    assert [line.qty_received for line in found.lines] == [Decimal(11), Decimal(8)]


def test_an_order_that_received_nothing_still_has_its_lines(receipts_path: Path) -> None:
    """Zero is the answer, and it is the most consequential one in the file.

    Dropping these lines would make "ordered and nothing came" indistinguishable
    from "no such line", and the matcher could not raise the reason code that
    holds the invoice.
    """
    found = _fetch("AP-NONE", receipts_path)

    assert len(found.lines) == 3
    assert all(line.qty_received == Decimal(0) for line in found.lines)
    assert found.is_empty is False


def test_an_unknown_order_is_an_empty_set_never_none(receipts_path: Path) -> None:
    """Nothing arrived is a fact. A None would be a lookup that failed."""
    found = _fetch("AP-NOBODY-HAS-THIS", receipts_path)

    assert found.po_number == "AP-NOBODY-HAS-THIS"
    assert found.lines == []
    assert found.is_empty is True


def test_nothing_received_and_nothing_known_are_different_answers(
    receipts_path: Path,
) -> None:
    """The distinction this whole module is built around, stated once."""
    nothing_came = _fetch("AP-NONE", receipts_path)
    never_asked = _fetch("AP-UNKNOWN", receipts_path)

    assert nothing_came.is_empty is False
    assert never_asked.is_empty is True
    assert nothing_came.quantity_for(1) == Decimal(0)
    assert never_asked.quantity_for(1) is None


# --- shape and joining ------------------------------------------------------


def test_lines_come_back_in_line_number_order(receipts_path: Path) -> None:
    """Sorted by the key they join on, whatever order the file listed them in."""
    found = _fetch("AP-FULL", receipts_path)
    assert [line.line_no for line in found.lines] == [1, 2]


def test_quantities_are_decimal_not_float(receipts_path: Path) -> None:
    found = _fetch("AP-FULL", receipts_path)
    assert all(isinstance(line.qty_received, Decimal) for line in found.lines)


def test_two_deliveries_against_one_line_are_summed(tmp_path: Path) -> None:
    """An order received in two drops is two documents against the same lines.

    The matcher's only question is how much arrived in total, so the summation
    happens here rather than in every caller.
    """
    first = _document("AP-SPLIT", "partial", [("10", "4")])
    second = _document("AP-SPLIT", "partial", [("10", "6")]) | {"received_on": "2026-08-20"}
    path = _receipts_file(tmp_path, [first, second])

    found = _fetch("AP-SPLIT", path)

    assert len(found.lines) == 1
    assert found.lines[0].qty_received == Decimal(10)
    # Dated by the later arrival: that is when the line became complete.
    assert found.lines[0].received_on.isoformat() == "2026-08-20"


def test_only_the_requested_order_is_returned(receipts_path: Path) -> None:
    assert _fetch("AP-FULL", receipts_path).po_number == "AP-FULL"
    assert len(_fetch("AP-FULL", receipts_path).lines) == 2


def test_the_tool_records_how_long_it_took(receipts_path: Path) -> None:
    output = get_receipts(GetReceiptsInput(po_number="AP-FULL", receipts_path=receipts_path))
    assert output.latency_ms >= 0


# --- the file itself failing ------------------------------------------------


def test_a_missing_file_is_an_error_not_an_empty_set(tmp_path: Path) -> None:
    """A configuration failure affecting every invoice, not a fact about one."""
    with pytest.raises(ReceiptsError, match="no receipts file"):
        _fetch("AP-FULL", tmp_path / "absent.json")


def test_broken_json_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "receipts.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ReceiptsError, match="not valid JSON"):
        _fetch("AP-FULL", path)


def test_a_file_with_no_receipts_key_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps({"note": "wrong shape"}), encoding="utf-8")

    with pytest.raises(ReceiptsError, match="declares no receipts"):
        _fetch("AP-FULL", path)


# --- joining by line_no, not by position ------------------------------------


def test_lines_join_on_their_declared_number(tmp_path: Path) -> None:
    """The key is written in the file, not implied by the order of a list."""
    document = _document("AP-NUM", "full", [("3", "3"), ("5", "5")])
    for index, line in enumerate(document["lines"], start=1):
        line["line_no"] = index
    path = _receipts_file(tmp_path, [document])

    found = _fetch("AP-NUM", path)

    assert [(line.line_no, line.qty_received) for line in found.lines] == [
        (1, Decimal(3)),
        (2, Decimal(5)),
    ]


def test_reordering_the_lines_does_not_move_the_quantities(tmp_path: Path) -> None:
    """The whole reason this stopped being positional.

    A reordered purchase order used to move every received quantity onto the
    wrong line, and the matcher would have reported a variance on a line that
    was fine - with nothing in the trail to say why.
    """
    document = _document("AP-REV", "full", [("3", "3"), ("5", "5")])
    document["lines"][0]["line_no"] = 1
    document["lines"][1]["line_no"] = 2
    document["lines"].reverse()
    path = _receipts_file(tmp_path, [document])

    found = _fetch("AP-REV", path)

    assert found.quantity_for(1) == Decimal(3)
    assert found.quantity_for(2) == Decimal(5)


def test_a_file_without_line_numbers_falls_back_to_the_item_name(tmp_path: Path) -> None:
    """The only other key both sides share, for a file written before the fix."""
    path = _receipts_file(tmp_path, [_document("AP-OLD", "full", [("3", "3"), ("5", "5")])])

    found = _fetch("AP-OLD", path)

    assert [line.line_no for line in found.lines] == [1, 2]


def test_a_line_with_neither_key_is_refused(tmp_path: Path) -> None:
    """Loud rather than positional: a silent guess puts a quantity on the wrong line."""
    path = _receipts_file(
        tmp_path,
        [
            {
                "receipt_id": "GR-AP-BAD",
                "po_number": "AP-BAD",
                "status": "full",
                "received_on": "2026-08-17",
                "lines": [{"qty_ordered": "3", "qty_received": "3"}],
            }
        ],
    )

    with pytest.raises(ReceiptsError, match="migrate_receipts"):
        _fetch("AP-BAD", path)


def test_the_shipped_receipts_file_carries_line_numbers() -> None:
    """The migration ran. A future seed writes them; this asserts the past one did."""
    path = get_settings().receipts_path
    if not path.is_file():  # pragma: no cover - data/ is git-ignored and absent in CI
        pytest.skip("no receipts file; data/ is git-ignored")
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for receipt in document["receipts"]:
        for line in receipt["lines"]:
            assert "line_no" in line, receipt["po_number"]
