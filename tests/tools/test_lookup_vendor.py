"""Resolving a printed vendor name to a record in the master.

The asymmetry this file is really about: matching the wrong vendor pays the
wrong party, and matching nobody asks a person a question. So every test that
pushes on the boundary pushes in the direction of asking.

Nothing here touches a network. The master is a committed YAML file, which is
the point of it being a file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ap_agent.contracts.vendor import VendorMatch, VendorMatchBasis
from ap_agent.tools.lookup_vendor import (
    LookupVendorInput,
    lookup_vendor,
    normalise_tax_id,
    normalise_vendor_name,
    remit_fingerprint,
    remit_matches,
)
from ap_agent.vendor_master import load_vendor_master

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


def test_no_remittance_comparison_is_claimed_without_a_remit_block() -> None:
    """Nothing printed to compare, so the honest answer is None, not False.

    False would mean a comparison ran and disagreed, which is a hard stop.
    None means nobody has compared anything, which is an open question.
    """
    assert _match("Deccan Print and Paper").remit_to_matches_master is None


# --- the remit-to block, compared and never returned ------------------------

ON_FILE = "XX00APAGENT0000000059"
"""Meridian's fictional account; the shipped master holds only its fingerprint."""


def _remit(display: str | None) -> bool | None:
    return lookup_vendor(
        LookupVendorInput(vendor_name="Meridian Office Supplies", remit_to_display=display)
    ).match.remit_to_matches_master


@pytest.mark.parametrize(
    "display",
    [
        "Meridian Office Supplies, IBAN XX00 APAG ENT0 0000 0005 9",
        "IBAN: xx00apagent0000000059",
        "Pay to IBAN XX00APAGENT0000000059 BIC MERIUS33",
    ],
)
def test_the_account_on_file_matches_however_it_is_spaced(display: str) -> None:
    assert _remit(display) is True


def test_a_spaced_iban_matches_an_unspaced_fingerprint() -> None:
    """Printed grouped in fours; stored as the SHA-256 of the run-together form.

    The fingerprint is computed here with hashlib directly, not through
    remit_fingerprint, so the test does not grade the function with itself.
    """
    stored = hashlib.sha256(b"GB29NWBK60161331926819").hexdigest()
    assert remit_matches("Remit to IBAN GB29 NWBK 6016 1331 9268 19", stored) is True
    assert remit_matches("iban: gb29 nwbk 6016 1331 9268 19", stored) is True


def test_every_shipped_fingerprint_is_of_the_normalised_account() -> None:
    """The master was fingerprinted in the same form the comparison hashes.

    Upper-case, no whitespace. If the two disagreed, every real account would
    read as a mismatch - so this is checked for all ten, not assumed.
    """
    for vendor in load_vendor_master():
        account = f"XX00APAGENT{int(vendor.erp_id):010d}"
        assert vendor.remit_account_sha256 == hashlib.sha256(account.encode()).hexdigest()
        assert vendor.remit_account_sha256 == remit_fingerprint(account.lower())
        assert vendor.remit_account_sha256 == remit_fingerprint(" ".join(account))


def test_a_different_account_is_a_mismatch() -> None:
    """The published test IBAN, standing in for "we have changed banks"."""
    assert _remit("Meridian Office Supplies, IBAN GB29 NWBK 6016 1331 9268 19") is False


PLANTED = "GB29NWBK60161331926819"
PLANTED_SPACED = "GB29 NWBK 6016 1331 9268 19"
ON_FILE_SPACED = "XX00 APAG ENT0 0000 0005 9"


@pytest.mark.parametrize(
    "display",
    [
        f"IBAN {ON_FILE} or IBAN {PLANTED}",
        f"IBAN {PLANTED} / IBAN {ON_FILE}",
        f"{ON_FILE_SPACED}, {PLANTED_SPACED}",
        f"{PLANTED_SPACED} {ON_FILE_SPACED}",
        f"{ON_FILE}\n{PLANTED}",
        f"{ON_FILE} {PLANTED}",
        f"Primary: {ON_FILE}. New account from 1 Nov: {PLANTED}",
    ],
)
def test_the_right_account_beside_a_second_one_is_a_mismatch(display: str) -> None:
    """A legitimate account printed next to a planted one is the attack.

    Every IBAN in the block must be the account on file. Order, spacing and
    separator make no difference - and nothing about the real account being
    there too can make the block pass.
    """
    assert _remit(display) is False


def test_an_account_the_pattern_cannot_split_fails_closed() -> None:
    """A known false hold, pinned so it stays in the safe direction.

    When an IBAN's body is a multiple of four characters, a following four-letter
    word ("BANK") reads as one more group, so the token is longer than the account
    and cannot match it. That holds a legitimate invoice for a person - the right
    way to be wrong - and HANDOFF.md records it. A per-country IBAN length table
    would split it; until then, this must never become True.
    """
    account = "XX00ABCDEFGHIJKLMNOPQRST"
    on_file = remit_fingerprint(account)
    assert remit_matches(f"IBAN {account}", on_file) is True
    assert remit_matches(f"IBAN {account} BANK OF X", on_file) is False


@pytest.mark.parametrize(
    "display",
    [
        None,
        "",
        "2140 Larkspur Commerce Way, Denver, CO 80216",
        "ACH routing 021000021, account 123456789",
    ],
)
def test_a_block_with_no_iban_compares_nothing(display: str | None) -> None:
    """No IBAN, nothing compared: None, never True.

    A postal address names no account, and a non-IBAN account - a US routing
    and account pair - is not recognised. None does not hold the invoice, so a
    changed US account is not caught by this check; HANDOFF.md records that.
    """
    assert _remit(display) is None


def test_nothing_on_file_compares_nothing(two_acmes: Path) -> None:
    """A master record with no fingerprint cannot say a printed account is wrong."""
    match = lookup_vendor(
        LookupVendorInput(
            vendor_name="x",
            vendor_tax_id="11-1111111",
            remit_to_display="IBAN GB29 NWBK 6016 1331 9268 19",
            master_path=two_acmes,
        )
    ).match
    assert match.resolved
    assert match.remit_to_matches_master is None


def test_the_printed_account_never_leaves_the_tool() -> None:
    """Only the boolean crosses back. The result carries no trace of the block."""
    printed = "IBAN GB29 NWBK 6016 1331 9268 19"
    dumped = lookup_vendor(
        LookupVendorInput(vendor_name="Meridian Office Supplies", remit_to_display=printed)
    ).model_dump_json()
    assert "GB29" not in dumped
    assert "NWBK" not in dumped


def test_the_tool_records_how_long_it_took() -> None:
    """The audit row needs it, and a lookup that got slow should be visible."""
    assert lookup_vendor(LookupVendorInput(vendor_name="Anything")).latency_ms >= 0
