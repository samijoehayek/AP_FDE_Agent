"""Seed the QuickBooks sandbox with vendors, items and purchase orders.

Matching needs three things: an invoice, the purchase order it claims to be
against, and evidence the goods arrived. QuickBooks Online models the first two
and **not the third** - it has no goods-receipt entity, so "what was actually
received" has no home in the ERP and has to be owned here. That is why this
script writes two files: a manifest of what was created in QuickBooks, and a
separate receipts file that exists only in this system.

Idempotent by query. Every create is preceded by a lookup on the natural key -
DisplayName for a vendor, Name for an item, DocNumber for a purchase order - and
skipped if something is already there. Running it twice creates nothing, which
matters because a sandbox is a shared, long-lived thing and there is no bulk
delete: a script that duplicated its seed on every run would poison the fixture
it exists to provide.

Usage:
    uv run python scripts/seed_sandbox.py --dry-run
    uv run python scripts/seed_sandbox.py
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, cast

import typer

from ap_agent.config import REPO_ROOT
from ap_agent.errors import APAgentError
from ap_agent.integrations.qbo.client import QboClient

app = typer.Typer(add_completion=False, help=__doc__)

OUT_DIR = REPO_ROOT / "data" / "generated"
MANIFEST_PATH = OUT_DIR / "seed_manifest.json"
RECEIPTS_PATH = OUT_DIR / "receipts.json"

SEED = 20260910
"""Fixed, so the same corpus comes out every time and a failure is reproducible."""


@dataclass(frozen=True)
class VendorSpec:
    """One supplier in the fixture."""

    display_name: str
    currency: str
    tax_id: str


@dataclass(frozen=True)
class ItemSpec:
    """One catalogue line."""

    name: str
    unit_price: Decimal
    description: str


@dataclass
class PoLine:
    """One purchase-order line, with its extension computed."""

    item: str
    qty: Decimal
    unit_price: Decimal
    amount: Decimal = field(init=False)

    def __post_init__(self) -> None:
        """Extend the line once, so quantity x price cannot drift from amount."""
        self.amount = (self.qty * self.unit_price).quantize(Decimal("0.01"))


VENDORS: tuple[VendorSpec, ...] = (
    VendorSpec("TechVision Distributors Pvt Ltd", "INR", "27AABCT1234F1Z5"),
    VendorSpec("Meridian Office Supplies", "USD", "84-1938472"),
    VendorSpec("Kestrel Components Ltd", "USD", "91-2274618"),
    VendorSpec("Sundar Electronics Trading", "INR", "29AAGCS8842K1ZP"),
    VendorSpec("Northwind Peripherals Inc", "USD", "27-6653019"),
    VendorSpec("Anand Cables and Connectors", "INR", "07AACCA5521M1Z8"),
    VendorSpec("Blue Harbour Furniture Co", "USD", "45-8871223"),
    VendorSpec("Deccan Print and Paper", "INR", "36AADCD7719L1ZQ"),
    VendorSpec("Orion Networking Supplies", "USD", "13-4429087"),
    VendorSpec("Vasanth Industrial Tools", "INR", "33AAHCV2298N1ZR"),
)

ITEMS: tuple[ItemSpec, ...] = (
    ItemSpec("Mechanical Keyboard TKL", Decimal("6450.00"), "Tenkeyless mechanical keyboard"),
    ItemSpec("27in IPS Monitor", Decimal("18900.00"), "27-inch 1440p IPS display"),
    ItemSpec("USB-C Dock 11-port", Decimal("9250.00"), "Docking station, 11 ports"),
    ItemSpec("Wireless Mouse Ergo", Decimal("3100.00"), "Ergonomic wireless mouse"),
    ItemSpec("Laser Printer Mono", Decimal("21400.00"), "Monochrome laser printer"),
    ItemSpec("A4 Copier Paper (box)", Decimal("2650.00"), "Box of 5 reams, 80gsm"),
    ItemSpec("Cat6 Patch Cable 3m", Decimal("420.00"), "Cat6 ethernet patch lead"),
    ItemSpec("Task Chair Mesh", Decimal("14750.00"), "Mesh-back adjustable task chair"),
    ItemSpec("Network Switch 24-port", Decimal("32800.00"), "Managed gigabit switch"),
    ItemSpec("External SSD 2TB", Decimal("15600.00"), "Portable NVMe SSD"),
    ItemSpec("Webcam 1080p", Decimal("5300.00"), "1080p60 conferencing webcam"),
    ItemSpec("Surge Protector 8-way", Decimal("1850.00"), "8-way surge-protected strip"),
)

RECEIPT_PLAN: tuple[str, ...] = (
    # Seven fully received, two partial, one not received at all. The mix is the
    # point: a matcher that only ever sees complete receipts is untested against
    # the two cases that actually generate exceptions in an AP function.
    "full",
    "full",
    "full",
    "full",
    "full",
    "full",
    "full",
    "partial",
    "partial",
    "none",
)


class SeedError(APAgentError):
    """Seeding the sandbox failed."""


@dataclass(frozen=True)
class SeedContext:
    """Facts about the target company that every create needs."""

    expense_account: str
    home_currency: str
    multicurrency: bool
    item_ids: dict[str, str]


@dataclass(frozen=True)
class PoDraft:
    """One purchase order, decided before anything is sent."""

    doc_number: str
    vendor: VendorSpec
    vendor_id: str
    order_date: str
    lines: list[PoLine]


def po_number(index: int) -> str:
    """Stable document number, which is also the idempotency key."""
    return f"AP-SEED-{index + 1:03d}"


def _created_id(response: dict[str, Any], entity: str) -> str:
    """Return the Id QuickBooks assigned to a record it just created."""
    record: dict[str, Any] = response.get(entity) or {}
    created: Any = record.get("Id")
    return str(created) if created is not None else ""


def _rows(response: dict[str, Any], entity: str) -> list[dict[str, Any]]:
    """Return the record list from a QuickBooks query response.

    QuickBooks omits the key entirely when nothing matches, rather than sending
    an empty list, so "no results" and "malformed response" look alike from the
    outside. Both are treated as no results here - a lookup that cannot find
    something is exactly the case the caller is prepared for.
    """
    envelope: dict[str, Any] = response.get("QueryResponse") or {}
    rows: Any = envelope.get(entity)
    if not isinstance(rows, list):
        return []
    return [cast("dict[str, Any]", row) for row in cast("list[Any]", rows) if isinstance(row, dict)]


def _first_id(response: dict[str, Any], entity: str) -> str | None:
    """Return the Id of the first record in a query response, if any."""
    rows = _rows(response, entity)
    if not rows:
        return None
    found: Any = rows[0].get("Id")
    return str(found) if found is not None else None


def _escape(value: str) -> str:
    """Escape a literal for a QuickBooks query string."""
    return value.replace("'", "\\'")


class Seeder:
    """Creates the fixture, skipping anything already present."""

    def __init__(self, client: QboClient, *, dry_run: bool = False) -> None:
        self.client = client
        self.dry_run = dry_run
        self.created: dict[str, int] = {"vendors": 0, "items": 0, "purchase_orders": 0}
        self.skipped: dict[str, int] = {"vendors": 0, "items": 0, "purchase_orders": 0}

    # --- lookups -------------------------------------------------------

    def _lookup(self, entity: str, field_name: str, value: str) -> str | None:
        """Find one record by its natural key, or None.

        Every idempotency check goes through here. The query is assembled from
        this module's own constants - vendor names, item names, generated
        document numbers - and never from anything read off a document, which is
        why interpolation is acceptable in a way it would not be downstream.
        Quotes are escaped regardless, because "no untrusted input reaches here"
        is a property that has to survive the next person editing the file.

        A dry run answers "nothing exists" without asking. Lookups are reads, so
        letting them through would be harmless in principle - but it would make
        ``--dry-run`` require live credentials, which defeats the point of having
        a mode that plans the work offline.
        """
        if self.dry_run:
            return None
        query = f"SELECT Id FROM {entity} WHERE {field_name} = '{_escape(value)}'"  # noqa: S608
        return _first_id(self.client.query(query), entity)

    def find_vendor(self, name: str) -> str | None:
        """Return the QuickBooks id of a vendor with this DisplayName."""
        return self._lookup("Vendor", "DisplayName", name)

    def find_item(self, name: str) -> str | None:
        """Return the QuickBooks id of an item with this Name."""
        return self._lookup("Item", "Name", name)

    def find_purchase_order(self, doc_number: str) -> str | None:
        """Return the QuickBooks id of a purchase order with this DocNumber."""
        return self._lookup("PurchaseOrder", "DocNumber", doc_number)

    def find_expense_account(self) -> str:
        """Any expense account will do for a fixture; take the first."""
        query = "SELECT Id FROM Account WHERE AccountType = 'Expense' MAXRESULTS 1"
        found = _first_id(self.client.query(query), "Account")
        if not found:
            msg = "the sandbox has no Expense account to book purchase-order lines against"
            raise SeedError(msg)
        return found

    def home_currency(self) -> str:
        """Read the company's home currency.

        Attaching a CurrencyRef that differs from home fails unless multicurrency
        is switched on, and it is off by default in a fresh sandbox. Asking first
        means the script degrades to home currency with a warning rather than
        failing ten creates in.
        """
        rows = _rows(self.client.query("SELECT * FROM CompanyInfo"), "CompanyInfo")
        if rows:
            ref: dict[str, Any] = rows[0].get("CurrencyRef") or {}
            currency: Any = ref.get("value")
            if currency:
                return str(currency)
        return "USD"

    # --- creates -------------------------------------------------------

    def vendor(self, spec: VendorSpec, ctx: SeedContext) -> str:
        """Create the vendor unless one with that DisplayName already exists."""
        existing = self.find_vendor(spec.display_name)
        if existing:
            self.skipped["vendors"] += 1
            return existing

        payload: dict[str, Any] = {
            "DisplayName": spec.display_name,
            "CompanyName": spec.display_name,
            "TaxIdentifier": spec.tax_id,
        }
        if ctx.multicurrency and spec.currency != ctx.home_currency:
            payload["CurrencyRef"] = {"value": spec.currency}

        if self.dry_run:
            self.created["vendors"] += 1
            return f"dry-run-vendor-{spec.display_name}"

        result = self.client.create("vendor", payload)
        self.created["vendors"] += 1
        return _created_id(result, "Vendor")

    def item(self, spec: ItemSpec, ctx: SeedContext) -> str:
        """Create the catalogue item unless one with that Name already exists."""
        existing = self.find_item(spec.name)
        if existing:
            self.skipped["items"] += 1
            return existing

        payload: dict[str, Any] = {
            "Name": spec.name,
            "Description": spec.description,
            "Type": "Service",
            "IncomeAccountRef": {"value": ctx.expense_account},
            "ExpenseAccountRef": {"value": ctx.expense_account},
        }
        if self.dry_run:
            self.created["items"] += 1
            return f"dry-run-item-{spec.name}"

        result = self.client.create("item", payload)
        self.created["items"] += 1
        return _created_id(result, "Item")

    def purchase_order(self, draft: PoDraft, ctx: SeedContext) -> str:
        """Create the purchase order unless that DocNumber already exists."""
        existing = self.find_purchase_order(draft.doc_number)
        if existing:
            self.skipped["purchase_orders"] += 1
            return existing

        payload: dict[str, Any] = {
            "DocNumber": draft.doc_number,
            "VendorRef": {"value": draft.vendor_id},
            "APAccountRef": {"value": ctx.expense_account},
            "TxnDate": draft.order_date,
            "Line": [
                {
                    "DetailType": "ItemBasedExpenseLineDetail",
                    "Amount": float(line.amount),
                    "Description": line.item,
                    "ItemBasedExpenseLineDetail": {
                        "ItemRef": {"value": ctx.item_ids[line.item], "name": line.item},
                        "Qty": float(line.qty),
                        "UnitPrice": float(line.unit_price),
                    },
                }
                for line in draft.lines
            ],
        }
        if ctx.multicurrency and draft.vendor.currency != ctx.home_currency:
            payload["CurrencyRef"] = {"value": draft.vendor.currency}

        if self.dry_run:
            self.created["purchase_orders"] += 1
            return f"dry-run-po-{draft.doc_number}"

        result = self.client.create("purchaseorder", payload)
        self.created["purchase_orders"] += 1
        return _created_id(result, "PurchaseOrder")


def build_lines(rng: random.Random, items: tuple[ItemSpec, ...]) -> list[PoLine]:
    """Two to four distinct lines with plausible quantities."""
    chosen = rng.sample(list(items), rng.randint(2, 4))
    return [
        PoLine(item=spec.name, qty=Decimal(rng.randint(1, 12)), unit_price=spec.unit_price)
        for spec in chosen
    ]


def build_receipts(manifest: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Derive goods receipts from the purchase orders.

    QuickBooks has no goods-receipt entity, so this file is the third leg of the
    three-way match and it belongs to ap-agent. Modelling it here rather than
    pretending the ERP holds it keeps the gap visible instead of discovering it
    when matching is written.
    """
    receipts: list[dict[str, Any]] = []
    for order, plan in zip(manifest["purchase_orders"], RECEIPT_PLAN, strict=True):
        lines: list[dict[str, Any]] = []
        for line in order["lines"]:
            ordered = Decimal(str(line["qty"]))
            if plan == "full":
                received = ordered
            elif plan == "none":
                received = Decimal(0)
            else:
                # Partial: short by 1 to 3 units, never below zero.
                received = max(Decimal(0), ordered - Decimal(rng.randint(1, 3)))
            lines.append(
                {"item": line["item"], "qty_ordered": str(ordered), "qty_received": str(received)}
            )

        receipts.append(
            {
                "receipt_id": f"GR-{order['doc_number']}",
                "po_number": order["doc_number"],
                "status": plan,
                "received_on": order["order_date"],
                "lines": lines,
            }
        )

    return {
        "note": (
            "QuickBooks Online has no goods-receipt entity. These records are owned by "
            "ap-agent and are the third leg of the three-way match."
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "receipts": receipts,
    }


@app.command()
def main(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan and write files without calling QuickBooks.")
    ] = False,
) -> None:
    """Create the sandbox fixture, skipping anything already present."""
    rng = random.Random(SEED)  # noqa: S311 - fixture generation, not crypto
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if dry_run:
        typer.secho("dry run: no QuickBooks calls will be made\n", fg=typer.colors.YELLOW)

    item_ids: dict[str, str] = {}
    try:
        client = QboClient()
        seeder = Seeder(client, dry_run=dry_run)
        ctx = SeedContext(
            expense_account="dry-run-account" if dry_run else seeder.find_expense_account(),
            home_currency="USD" if dry_run else seeder.home_currency(),
            multicurrency=not dry_run,
            item_ids=item_ids,
        )
    except APAgentError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    vendor_ids: dict[str, str] = {}
    orders: list[dict[str, Any]] = []

    try:
        typer.echo("vendors")
        for spec in VENDORS:
            vendor_ids[spec.display_name] = seeder.vendor(spec, ctx)

        typer.echo("items")
        for spec in ITEMS:
            item_ids[spec.name] = seeder.item(spec, ctx)

        typer.echo("purchase orders")
        base_date = datetime.now(UTC).date() - timedelta(days=45)
        for index, spec in enumerate(VENDORS):
            draft = PoDraft(
                doc_number=po_number(index),
                vendor=spec,
                vendor_id=vendor_ids[spec.display_name],
                order_date=(base_date + timedelta(days=index * 3)).isoformat(),
                lines=build_lines(rng, ITEMS),
            )
            po_id = seeder.purchase_order(draft, ctx)
            orders.append(
                {
                    "qbo_id": po_id,
                    "doc_number": draft.doc_number,
                    "vendor": spec.display_name,
                    "vendor_qbo_id": draft.vendor_id,
                    "currency": spec.currency if ctx.multicurrency else ctx.home_currency,
                    "order_date": draft.order_date,
                    "total": str(sum(line.amount for line in draft.lines)),
                    "lines": [
                        {
                            "item": line.item,
                            "qty": str(line.qty),
                            "unit_price": str(line.unit_price),
                            "amount": str(line.amount),
                        }
                        for line in draft.lines
                    ],
                }
            )
    except APAgentError as exc:
        typer.secho(f"\nseeding stopped: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": "dry-run" if dry_run else "sandbox",
        "home_currency": ctx.home_currency,
        "multicurrency": ctx.multicurrency,
        "vendors": [
            {
                "qbo_id": vendor_ids[v.display_name],
                "display_name": v.display_name,
                "currency": v.currency,
                "tax_id": v.tax_id,
            }
            for v in VENDORS
        ],
        "items": [
            {"qbo_id": item_ids[i.name], "name": i.name, "unit_price": str(i.unit_price)}
            for i in ITEMS
        ],
        "purchase_orders": orders,
    }

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    RECEIPTS_PATH.write_text(
        json.dumps(build_receipts(manifest, rng), indent=2) + "\n", encoding="utf-8"
    )

    typer.echo()
    for kind in ("vendors", "items", "purchase_orders"):
        typer.echo(
            f"  {kind:<16} created {seeder.created[kind]:>2}   skipped {seeder.skipped[kind]:>2}"
        )
    typer.secho(f"\nwrote {MANIFEST_PATH.relative_to(REPO_ROOT)}", fg=typer.colors.GREEN)
    typer.secho(f"wrote {RECEIPTS_PATH.relative_to(REPO_ROOT)}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
