"""Resolving a printed vendor name to a record in the master.

The asymmetry this file is really about: matching the wrong vendor pays the
wrong party, and matching nobody asks a person a question. So every test that
pushes on the boundary pushes in the direction of asking.

Nothing here touches a network. The master is a committed YAML file, which is
the point of it being a file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ap_agent.contracts.vendor import VendorMatch, VendorMatchBasis
from ap_agent.tools.lookup_vendor import (
    LookupVendorInput,
    lookup_vendor,
    normalise_tax_id,
    normalise_vendor_name,
)

TWO_ACMES = """
version: "v1"
vendors:
  - display_name: "Acme Ltd"
    erp_id: "70"
    currency: "USD"
    tax_id: "11-1111111"
    country: "US"
    address: ["1 One Street"]
  - display_name: "ACME Limited"
    erp_id: "71"
    currency: "GBP"
    tax_id: "22-2222222"
    country: "GB"
    address: ["2 Two Street"]
"""


@pytest.fixture
def two_acmes(tmp_path: Path) -> Path:
    """A master where two real suppliers normalise to the same name."""
    path = tmp_path / "vendor_master.yaml"
    path.write_text(TWO_ACMES, encoding="utf-8")
    return path


def _match(name: str, tax_id: str | None = None, master: Path | None = None) -> VendorMatch:
    return lookup_vendor(
        LookupVendorInput(vendor_name=name, vendor_tax_id=tax_id, master_path=master)
    ).match


# --- normalisation, which is its own function for a reason -------------------


@pytest.mark.parametrize(
    ("printed", "normalised"),
    [
        ("Acme", "acme"),
        ("ACME", "acme"),
        ("Acme Ltd", "acme"),
        ("ACME Limited", "acme"),
        ("Acme Ltd.", "acme"),
        ("Acme, Inc.", "acme"),
        ("Acme  Widgets   Ltd", "acme widgets"),
        ("TechVision Distributors Pvt Ltd", "techvision distributors"),
        ("Blue Harbour Furniture Co", "blue harbour furniture"),
        ("Kestrel Components Ltd", "kestrel components"),
    ],
)
def test_each_normalisation_rule(printed: str, normalised: str) -> None:
    """Case, punctuation, whitespace and legal suffix are rendering, not identity."""
    assert normalise_vendor_name(printed) == normalised


def test_a_suffix_in_the_middle_of_a_name_survives() -> None:
    """Only trailing suffixes go. "Limited Editions" is a name, not a suffix."""
    assert normalise_vendor_name("Limited Editions Ltd") == "limited editions"


def test_several_trailing_suffixes_all_go() -> None:
    assert normalise_vendor_name("Acme Pvt Ltd") == "acme"


def test_a_name_that_is_nothing_but_suffixes_normalises_to_empty() -> None:
    """And an empty result must never match anything - see the lookup test."""
    assert normalise_vendor_name("Ltd.") == ""


@pytest.mark.parametrize(
    ("printed", "normalised"),
    [
        ("84-1938472", "841938472"),
        ("84 1938472", "841938472"),
        ("27AABCT1234F1Z5", "27aabct1234f1z5"),
    ],
)
def test_tax_ids_normalise_to_their_characters(printed: str, normalised: str) -> None:
    assert normalise_tax_id(printed) == normalised


# --- the two tiers -----------------------------------------------------------


def test_a_tax_id_resolves_the_vendor() -> None:
    match = _match("Anything At All", "27AABCT1234F1Z5")
    assert match.match_basis is VendorMatchBasis.TAX_ID_EXACT
    assert match.vendor_name == "TechVision Distributors Pvt Ltd"
    assert match.country == "IN"


def test_the_tax_id_wins_over_the_name() -> None:
    """The government issued one of these and a template author typed the other."""
    match = _match("Meridian Office Supplies", "27AABCT1234F1Z5")
    assert match.vendor_name == "TechVision Distributors Pvt Ltd"


def test_a_punctuated_tax_id_still_resolves() -> None:
    assert _match("x", "84 1938472").match_basis is VendorMatchBasis.TAX_ID_EXACT


def test_a_name_resolves_when_there_is_no_tax_id() -> None:
    match = _match("Kestrel Components Limited")
    assert match.match_basis is VendorMatchBasis.NAME_EXACT
    assert match.vendor_id == "60"


def test_the_same_supplier_under_two_renderings_resolves_to_one_vendor() -> None:
    """The duplicate-payment failure this normalisation exists to prevent."""
    first = _match("BLUE HARBOUR FURNITURE CO.")
    second = _match("Blue Harbour Furniture Company")
    assert first.vendor_id == second.vendor_id
    assert first.match_basis is VendorMatchBasis.NAME_EXACT


def test_a_resolved_match_carries_the_whole_identity() -> None:
    """The country in particular: it is what settles a DD/MM against a MM/DD."""
    match = _match("Vasanth Industrial Tools")
    assert match.vendor_id
    assert match.vendor_name
    assert match.country == "IN"
    assert match.currency == "INR"


# --- refusing to guess -------------------------------------------------------


def test_an_unknown_vendor_is_none_with_no_candidates() -> None:
    """No fuzzy tier at all, so there is nothing to shortlist."""
    match = _match("Nobody Has Heard Of This Company")
    assert match.match_basis is VendorMatchBasis.NONE
    assert match.candidates == []
    assert match.vendor_id is None


def test_an_unknown_tax_id_falls_through_to_the_name() -> None:
    """A tax id that matches nothing is not an answer, it is a tier that missed."""
    match = _match("Orion Networking Supplies", "99-9999999")
    assert match.match_basis is VendorMatchBasis.NAME_EXACT


def test_two_candidates_refuse_to_choose_and_list_both(two_acmes: Path) -> None:
    """A ranking here would be an uncalibrated number deciding who gets paid."""
    match = _match("Acme", master=two_acmes)
    assert match.match_basis is VendorMatchBasis.NONE
    assert {c.vendor_id for c in match.candidates} == {"70", "71"}
    assert match.vendor_id is None


def test_an_ambiguous_name_still_resolves_by_tax_id(two_acmes: Path) -> None:
    """Ambiguity is about the name. The tax id was never ambiguous."""
    match = _match("Acme", "22-2222222", master=two_acmes)
    assert match.match_basis is VendorMatchBasis.TAX_ID_EXACT
    assert match.vendor_id == "71"


def test_a_name_that_normalises_to_nothing_matches_nothing() -> None:
    """Otherwise every master vendor whose name is only a suffix would collide."""
    match = _match("Ltd.")
    assert match.match_basis is VendorMatchBasis.NONE


# --- what this tool must never carry ----------------------------------------


def test_no_remittance_comparison_is_claimed() -> None:
    """The master holds no bank details, so the honest answer is None, not False.

    False would mean a comparison ran and disagreed, which is a hard stop.
    None means nobody has compared anything, which is an open question.
    """
    assert _match("Deccan Print and Paper").bank_details_match_on_file is None


def test_the_tool_records_how_long_it_took() -> None:
    """The audit row needs it, and a lookup that got slow should be visible."""
    assert lookup_vendor(LookupVendorInput(vendor_name="Anything")).latency_ms >= 0
