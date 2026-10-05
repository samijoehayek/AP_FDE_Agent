"""One file defines the vendors, and it fails loudly rather than guessing.

The country in this file decides how an ambiguous date is read, and the date
decides the due date. A loader that filled in a default for a missing country
would make that decision silently, so every absence here is an error with the
vendor's name in it.

The shipped file is also under test: the seeder numbers purchase orders by a
vendor's position in it, so its order and its contents are part of the fixture
rather than incidental.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from ap_agent.vendor_master import (
    VENDOR_MASTER_PATH,
    MasterVendor,
    VendorMasterError,
    index_by_name,
    load_vendor_master,
    require_vendor,
)
from scripts.seed_sandbox import VENDORS, po_number

ONE_VENDOR = """
version: "v1"
vendors:
  - display_name: "Kestrel Components Ltd"
    erp_id: "60"
    currency: "usd"
    tax_id: "91-2274618"
    country: "us"
    address:
      - "805 Foundry Street"
      - "Tacoma, WA 98421"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "vendor_master.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- the shipped file -------------------------------------------------------


def test_the_shipped_master_covers_every_seeded_vendor() -> None:
    assert len(VENDORS) == 10
    assert len({v.display_name for v in VENDORS}) == 10


def test_every_vendor_has_a_country_and_an_address() -> None:
    """The two facts the seed manifest does not carry and this file exists for."""
    for vendor in load_vendor_master():
        assert len(vendor.country) == 2, vendor.display_name
        assert vendor.country.isupper(), vendor.display_name
        assert vendor.address, vendor.display_name


def test_the_country_agrees_with_the_tax_identifier() -> None:
    """A 15-character GSTIN is Indian; an NN-NNNNNNN EIN is US.

    The countries were read off the tax identifiers already in the fixture
    rather than chosen, and this is what keeps that true as vendors are added.
    """
    gstin_length = 15
    for vendor in load_vendor_master():
        if len(vendor.tax_id) == gstin_length and vendor.tax_id[:2].isdigit():
            assert vendor.country == "IN", vendor.display_name
        else:
            assert vendor.country == "US", vendor.display_name


def test_the_master_holds_no_bank_details() -> None:
    """Rule 2 of CLAUDE.md. The master holds a fingerprint of the account, never the account.

    Checked on the parsed data rather than the raw file, because the file's own
    comments explain what an IBAN is. No key may name a bank detail, no value may
    be IBAN-shaped or an account-length digit run, and the one remittance field is
    a SHA-256 - equality is all the comparison needs.
    """
    raw: Any = yaml.safe_load(VENDOR_MASTER_PATH.read_text(encoding="utf-8"))
    iban = re.compile(r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}\b")
    for vendor in cast("list[dict[str, Any]]", raw["vendors"]):
        for key, value in vendor.items():
            assert not re.search(r"iban|swift|bic|sort_code|account_number|routing", key), key
            values = cast("list[Any]", value) if isinstance(value, list) else [value]
            for text in (str(item) for item in values):
                if key == "remit_account_sha256":
                    assert re.fullmatch(r"[0-9a-f]{64}", text)
                    continue
                assert not iban.search(text), (key, text)
                assert not re.search(r"\b[0-9]{8,17}\b", text), (key, text)


def test_every_shipped_vendor_has_a_remittance_fingerprint() -> None:
    """Without one, the remit-to comparison is None for that vendor: nothing to compare."""
    assert all(vendor.remit_account_sha256 for vendor in load_vendor_master())


def test_vendor_order_is_what_numbers_the_purchase_orders() -> None:
    """AP-SEED-001 belongs to the first vendor listed. Reordering renumbers."""
    assert po_number(0) == "AP-SEED-001"
    assert VENDORS[0].display_name == load_vendor_master()[0].display_name


# --- loading ----------------------------------------------------------------


def test_a_remittance_fingerprint_must_be_a_sha256(tmp_path: Path) -> None:
    """Anything else - an account number pasted in by mistake above all - is refused."""
    body = ONE_VENDOR.replace(
        '    erp_id: "60"\n',
        '    erp_id: "60"\n    remit_account_sha256: "GB29NWBK60161331926819"\n',
    )
    with pytest.raises(VendorMasterError, match="remit_account_sha256"):
        load_vendor_master(_write(tmp_path, body))


def test_a_vendor_without_a_fingerprint_loads_with_none(tmp_path: Path) -> None:
    assert load_vendor_master(_write(tmp_path, ONE_VENDOR))[0].remit_account_sha256 is None


def test_codes_are_normalised_to_upper_case(tmp_path: Path) -> None:
    vendor = load_vendor_master(_write(tmp_path, ONE_VENDOR))[0]
    assert vendor.currency == "USD"
    assert vendor.country == "US"


def test_the_address_block_appends_the_country(tmp_path: Path) -> None:
    vendor = load_vendor_master(_write(tmp_path, ONE_VENDOR))[0]
    assert vendor.address_block == ("805 Foundry Street", "Tacoma, WA 98421", "US")


def test_a_vendor_is_frozen(tmp_path: Path) -> None:
    vendor = load_vendor_master(_write(tmp_path, ONE_VENDOR))[0]
    with pytest.raises(AttributeError):
        vendor.country = "GB"  # type: ignore[misc]


# --- failing loudly ---------------------------------------------------------


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(VendorMasterError, match="no vendor master"):
        load_vendor_master(tmp_path / "absent.yaml")


def test_a_file_with_no_vendors_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(VendorMasterError, match="declares no vendors"):
        load_vendor_master(_write(tmp_path, "version: v1\nvendors: []\n"))


def test_broken_yaml_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(VendorMasterError, match="not valid YAML"):
        load_vendor_master(_write(tmp_path, "vendors: [oops\n"))


@pytest.mark.parametrize(
    "field", ["display_name", "erp_id", "currency", "tax_id", "country", "address"]
)
def test_a_missing_field_names_the_vendor_and_the_field(tmp_path: Path, field: str) -> None:
    """The error has to say what to add and where, or it is just a stack trace."""
    record: dict[str, object] = {
        "display_name": "Kestrel Components Ltd",
        "erp_id": "60",
        "currency": "USD",
        "tax_id": "91-2274618",
        "country": "US",
        "address": ["805 Foundry Street"],
    }
    del record[field]
    body = yaml.safe_dump({"version": "v1", "vendors": [record]})

    with pytest.raises(VendorMasterError, match=field):
        load_vendor_master(_write(tmp_path, body))


def test_an_address_that_is_not_a_list_is_refused(tmp_path: Path) -> None:
    body = yaml.safe_dump(
        {
            "version": "v1",
            "vendors": [
                {
                    "display_name": "Kestrel Components Ltd",
                    "erp_id": "60",
                    "currency": "USD",
                    "tax_id": "91-2274618",
                    "country": "US",
                    "address": "805 Foundry Street, Tacoma",
                }
            ],
        }
    )
    with pytest.raises(VendorMasterError, match="list of lines"):
        load_vendor_master(_write(tmp_path, body))


def test_a_country_that_is_not_two_letters_is_refused(tmp_path: Path) -> None:
    """ISO-3166-1 alpha-2 is what the date-resolution rule reads."""
    body = ONE_VENDOR.replace('country: "us"', 'country: "USA"')
    with pytest.raises(VendorMasterError, match="alpha-2"):
        load_vendor_master(_write(tmp_path, body))


def test_a_duplicated_vendor_is_refused(tmp_path: Path) -> None:
    body = ONE_VENDOR + ONE_VENDOR.split("vendors:")[1]
    with pytest.raises(VendorMasterError, match="more than once"):
        load_vendor_master(_write(tmp_path, body))


def test_requiring_an_unknown_vendor_says_where_to_add_it(tmp_path: Path) -> None:
    vendors = index_by_name(load_vendor_master(_write(tmp_path, ONE_VENDOR)))
    with pytest.raises(VendorMasterError, match=r"sandbox_vendor_master\.yaml"):
        require_vendor(vendors, "Somebody Else Ltd")


def test_requiring_a_known_vendor_returns_it(tmp_path: Path) -> None:
    vendors = index_by_name(load_vendor_master(_write(tmp_path, ONE_VENDOR)))
    found = require_vendor(vendors, "Kestrel Components Ltd")
    assert isinstance(found, MasterVendor)
    assert found.country == "US"
