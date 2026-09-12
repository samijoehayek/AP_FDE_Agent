"""Load the sandbox vendor master from ``config/sandbox_vendor_master.yaml``.

One file defines the vendors. ``seed_sandbox.py`` builds the fixture it sends to
QuickBooks from this loader, and ``generate_invoices.py`` renders a vendor block
and a remittance block from the same records, so the two can never describe the
same supplier differently.

The country is the reason this module exists rather than a dict in one script.
``09/03/2024`` on an Indian invoice is 9 March and on a US invoice is 3
September, the document does not say which, and
``compute_extraction_confidence`` resolves it from the vendor's country. A
generated invoice whose vendor has no country cannot exercise that rule at all.

Two things this module deliberately does not do:

* **It holds no bank details and never will.** Rule 2 of CLAUDE.md gives
  remittance exactly one home - the vendor master, compared server-side - and
  the postal address here is what a remit-to block may print. An account number
  in this file would put one in a fixture, in a script, and on a rendered page.
* **It does not resolve a vendor for the pipeline.** That is ``lookup_vendor``,
  which does not exist yet, and which will have to decide what a *near* match on
  a name means. This module only reads a file and hands back what is in it. It
  sits beside ``guardrails/config.py`` because the pattern is already
  established: versioned data in ``config/``, the loader that validates it in
  ``src/ap_agent/`` - and because a script run as ``python scripts/x.py`` cannot
  import another script.

Missing data fails loudly. A vendor the master does not know is a fixture that
would have to be invented, and inventing it is how a country - which decides a
date, which decides a due date - becomes a guess nobody recorded making.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import yaml

from ap_agent.config import REPO_ROOT
from ap_agent.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

VENDOR_MASTER_PATH = REPO_ROOT / "config" / "sandbox_vendor_master.yaml"
"""The one definition of the sandbox vendors, committed and reviewable."""

REQUIRED_FIELDS = ("display_name", "erp_id", "currency", "tax_id", "country", "address")

COUNTRY_CODE_LENGTH = 2
"""ISO-3166-1 alpha-2. The date-resolution rule reads nothing else."""


class VendorMasterError(ConfigurationError):
    """The vendor master is missing, malformed, or does not cover a vendor."""


@dataclass(frozen=True)
class MasterVendor:
    """One supplier as the vendor master knows it.

    ``address`` is a tuple of display lines, in the order they are printed. The
    file stores it as a list so it reads naturally; it is frozen here because a
    fixture that can be edited in place by one caller is not a fixture.
    """

    display_name: str
    erp_id: str
    """The vendor's id in the ERP.

    The key the purchase-order identity check compares. A PO names its vendor by
    the ERP's id, and an invoice quoting another supplier's PO number has to be
    caught by comparing ids - names are what the document is claiming, and the
    claim is the thing under suspicion.
    """

    currency: str
    tax_id: str
    country: str
    address: tuple[str, ...]

    @property
    def address_block(self) -> tuple[str, ...]:
        """The postal address with the country appended, as printed."""
        return (*self.address, self.country)


def _require(record: dict[str, Any], index: int) -> MasterVendor:
    """Build one vendor, naming exactly what is missing if anything is."""
    missing = [field for field in REQUIRED_FIELDS if not record.get(field)]
    if missing:
        name = record.get("display_name") or f"vendor #{index + 1}"
        msg = f"{VENDOR_MASTER_PATH.name}: {name} is missing {', '.join(missing)}"
        raise VendorMasterError(msg)

    country = str(record["country"]).strip().upper()
    if len(country) != COUNTRY_CODE_LENGTH:
        msg = (
            f"{VENDOR_MASTER_PATH.name}: {record['display_name']} has country "
            f"{country!r}; ISO-3166-1 alpha-2 is two letters"
        )
        raise VendorMasterError(msg)

    address = record["address"]
    if not isinstance(address, list):
        msg = f"{VENDOR_MASTER_PATH.name}: {record['display_name']} address must be a list of lines"
        raise VendorMasterError(msg)

    return MasterVendor(
        display_name=str(record["display_name"]),
        erp_id=str(record["erp_id"]),
        currency=str(record["currency"]).strip().upper(),
        tax_id=str(record["tax_id"]),
        country=country,
        address=tuple(str(line) for line in cast("list[Any]", address)),
    )


def load_vendor_master(path: Path | None = None) -> tuple[MasterVendor, ...]:
    """Return every vendor in the master, in file order.

    File order is load-bearing: ``seed_sandbox.py`` numbers purchase orders by
    the vendor's index, so ``AP-SEED-001`` belongs to the first vendor listed.
    Reordering this file renumbers the fixture.

    Raises:
        VendorMasterError: The file is absent, unparseable, empty, or a record
            is missing a required field.
    """
    source = path or VENDOR_MASTER_PATH
    if not source.is_file():
        msg = f"no vendor master at {source}"
        raise VendorMasterError(msg)

    try:
        loaded: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"{source.name} is not valid YAML: {exc}"
        raise VendorMasterError(msg) from exc

    if not isinstance(loaded, dict):
        msg = f"{source.name} must be a mapping with a `vendors` key"
        raise VendorMasterError(msg)

    records: Any = cast("dict[str, Any]", loaded).get("vendors")
    if not isinstance(records, list) or not records:
        msg = f"{source.name} declares no vendors"
        raise VendorMasterError(msg)

    vendors = tuple(
        _require(cast("dict[str, Any]", record), index)
        for index, record in enumerate(cast("list[Any]", records))
    )

    names = [vendor.display_name for vendor in vendors]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        msg = f"{source.name} lists these vendors more than once: {duplicated}"
        raise VendorMasterError(msg)

    return vendors


def index_by_name(vendors: tuple[MasterVendor, ...]) -> dict[str, MasterVendor]:
    """Return the master keyed by display name, which is the join key."""
    return {vendor.display_name: vendor for vendor in vendors}


def require_vendor(vendors: dict[str, MasterVendor], display_name: str) -> MasterVendor:
    """Return the master record for ``display_name``, or fail saying so.

    Loud on purpose. The alternative - a placeholder country, an empty address -
    puts a value nobody chose onto a rendered page and into a truth file that
    later tests assert against.

    Raises:
        VendorMasterError: No vendor of that name is in the master.
    """
    found = vendors.get(display_name)
    if found is None:
        msg = (
            f"{display_name!r} is not in {VENDOR_MASTER_PATH.name}. "
            f"The vendor master defines the vendors; add it there rather than "
            f"inventing a country and an address here."
        )
        raise VendorMasterError(msg)
    return found


__all__ = [
    "REQUIRED_FIELDS",
    "VENDOR_MASTER_PATH",
    "MasterVendor",
    "VendorMasterError",
    "index_by_name",
    "load_vendor_master",
    "require_vendor",
]
