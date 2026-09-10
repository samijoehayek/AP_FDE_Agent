"""Seeding the sandbox: idempotency, and the receipts QuickBooks cannot hold.

A sandbox is shared and long-lived and has no bulk delete, so a seeder that
duplicated its fixture on every run would poison the thing it exists to provide.
Idempotency is the property under test.

No live calls: the QuickBooks client is replaced by an in-memory double that
records every request.
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any

import pytest

from scripts import seed_sandbox
from scripts.seed_sandbox import (
    ITEMS,
    RECEIPT_PLAN,
    VENDORS,
    PoLine,
    SeedContext,
    Seeder,
    build_receipts,
)


class FakeQbo:
    """An in-memory QuickBooks. Remembers what was created and answers queries."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.creates: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self._next_id = 1

    def query(self, statement: str) -> dict[str, Any]:
        self.queries.append(statement)
        if "CompanyInfo" in statement:
            return {"QueryResponse": {"CompanyInfo": [{"CurrencyRef": {"value": "USD"}}]}}
        if "AccountType = 'Expense'" in statement:
            return {"QueryResponse": {"Account": [{"Id": "33"}]}}

        entity = statement.split(" FROM ")[1].split(maxsplit=1)[0]
        value = statement.split("= '")[1].rstrip("'") if "= '" in statement else ""
        matches = [
            record
            for record in self.records.get(entity, [])
            if value in (record.get("DisplayName"), record.get("Name"), record.get("DocNumber"))
        ]
        return {"QueryResponse": {entity: matches}} if matches else {"QueryResponse": {}}

    def create(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.creates.append((entity, payload))
        name = {"vendor": "Vendor", "item": "Item", "purchaseorder": "PurchaseOrder"}[entity]
        stored = {**payload, "Id": str(self._next_id)}
        self._next_id += 1
        self.records.setdefault(name, []).append(stored)
        return {name: stored}


def _seed_once(fake: FakeQbo) -> Seeder:
    """Run the same sequence the script runs, against the double."""
    seeder = Seeder(fake)  # type: ignore[arg-type]
    item_ids: dict[str, str] = {}
    ctx = SeedContext(
        expense_account=seeder.find_expense_account(),
        home_currency=seeder.home_currency(),
        multicurrency=True,
        item_ids=item_ids,
    )
    vendor_ids = {spec.display_name: seeder.vendor(spec, ctx) for spec in VENDORS}
    for spec in ITEMS:
        item_ids[spec.name] = seeder.item(spec, ctx)

    for index, spec in enumerate(VENDORS):
        draft = seed_sandbox.PoDraft(
            doc_number=seed_sandbox.po_number(index),
            vendor=spec,
            vendor_id=vendor_ids[spec.display_name],
            order_date="2026-08-01",
            lines=[
                PoLine(
                    item=ITEMS[0].name, qty=seed_sandbox.Decimal(3), unit_price=ITEMS[0].unit_price
                )
            ],
        )
        seeder.purchase_order(draft, ctx)
    return seeder


# --- idempotency ------------------------------------------------------------


def test_a_first_run_creates_the_whole_fixture() -> None:
    fake = FakeQbo()
    seeder = _seed_once(fake)
    assert seeder.created == {"vendors": 10, "items": 12, "purchase_orders": 10}
    assert seeder.skipped == {"vendors": 0, "items": 0, "purchase_orders": 0}


def test_a_second_run_creates_nothing() -> None:
    """The property that makes this safe to run against a shared sandbox."""
    fake = FakeQbo()
    _seed_once(fake)
    creates_after_first = len(fake.creates)

    second = _seed_once(fake)

    assert second.created == {"vendors": 0, "items": 0, "purchase_orders": 0}
    assert second.skipped == {"vendors": 10, "items": 12, "purchase_orders": 10}
    assert len(fake.creates) == creates_after_first


def test_every_create_is_preceded_by_a_lookup() -> None:
    """Idempotency by query, not by hoping the sandbox is empty."""
    fake = FakeQbo()
    _seed_once(fake)
    natural_keys = ("DisplayName", "Name", "DocNumber")
    lookups = [q for q in fake.queries if any(key in q for key in natural_keys)]
    assert len(lookups) >= len(fake.creates)


def test_the_document_number_is_the_purchase_order_key() -> None:
    fake = FakeQbo()
    _seed_once(fake)
    po_queries = [q for q in fake.queries if "FROM PurchaseOrder" in q]
    assert all("DocNumber" in q for q in po_queries)
    assert "AP-SEED-001" in " ".join(po_queries)


def test_quotes_in_a_name_are_escaped() -> None:
    fake = FakeQbo()
    Seeder(fake).find_vendor("O'Brien Supplies")  # type: ignore[arg-type]
    assert "O\\'Brien" in fake.queries[0]


def test_a_dry_run_makes_no_calls_at_all() -> None:
    """--dry-run must not need credentials, so it may not even read."""
    fake = FakeQbo()
    seeder = Seeder(fake, dry_run=True)  # type: ignore[arg-type]
    ctx = SeedContext("acct", "USD", multicurrency=False, item_ids={})
    seeder.vendor(VENDORS[0], ctx)
    assert fake.queries == []
    assert fake.creates == []


# --- what QuickBooks cannot model -------------------------------------------


def _manifest_from(orders: int = 10, lines_each: int = 3) -> dict[str, Any]:
    return {
        "purchase_orders": [
            {
                "doc_number": f"AP-SEED-{i + 1:03d}",
                "order_date": "2026-08-01",
                "lines": [
                    {"item": f"Item {j}", "qty": "10", "unit_price": "5.00", "amount": "50.00"}
                    for j in range(lines_each)
                ],
            }
            for i in range(orders)
        ]
    }


def test_receipts_cover_every_purchase_order() -> None:
    generated = build_receipts(_manifest_from(), random.Random(1))
    assert len(generated["receipts"]) == 10
    assert {r["po_number"] for r in generated["receipts"]} == {
        f"AP-SEED-{i + 1:03d}" for i in range(10)
    }


def test_the_receipt_mix_is_seven_full_two_partial_one_none() -> None:
    """A matcher that only ever sees complete receipts is untested where it counts."""
    generated = build_receipts(_manifest_from(), random.Random(1))
    assert Counter(r["status"] for r in generated["receipts"]) == {
        "full": 7,
        "partial": 2,
        "none": 1,
    }
    assert Counter(RECEIPT_PLAN) == {"full": 7, "partial": 2, "none": 1}


def test_a_full_receipt_matches_the_ordered_quantity() -> None:
    generated = build_receipts(_manifest_from(), random.Random(1))
    full = next(r for r in generated["receipts"] if r["status"] == "full")
    assert all(line["qty_received"] == line["qty_ordered"] for line in full["lines"])


def test_a_partial_receipt_is_short_but_never_negative() -> None:
    generated = build_receipts(_manifest_from(), random.Random(1))
    partial = next(r for r in generated["receipts"] if r["status"] == "partial")
    for line in partial["lines"]:
        assert 0 <= int(line["qty_received"]) < int(line["qty_ordered"])


def test_nothing_is_received_on_the_unreceived_order() -> None:
    generated = build_receipts(_manifest_from(), random.Random(1))
    none = next(r for r in generated["receipts"] if r["status"] == "none")
    assert all(int(line["qty_received"]) == 0 for line in none["lines"])


def test_the_file_says_who_owns_receipts() -> None:
    """QuickBooks has no goods-receipt entity; this data is ours."""
    generated = build_receipts(_manifest_from(), random.Random(1))
    assert "no goods-receipt entity" in generated["note"]
    assert "ap-agent" in generated["note"]


# --- the fixture itself -----------------------------------------------------


def test_the_fixture_has_the_promised_shape() -> None:
    assert len(VENDORS) == 10
    assert len(ITEMS) == 12
    assert {v.currency for v in VENDORS} == {"INR", "USD"}
    assert len({v.display_name for v in VENDORS}) == 10
    assert len({i.name for i in ITEMS}) == 12


def test_a_line_extends_itself() -> None:
    line = PoLine(item="x", qty=seed_sandbox.Decimal(3), unit_price=seed_sandbox.Decimal("2.50"))
    assert str(line.amount) == "7.50"


@pytest.mark.parametrize("index", range(10))
def test_document_numbers_are_stable_and_unique(index: int) -> None:
    assert seed_sandbox.po_number(index) == f"AP-SEED-{index + 1:03d}"
