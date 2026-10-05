"""Resolve an extracted vendor name to a record in the vendor master.

Caller: code. The orchestrator calls this; no model can.
Side effects: LOCAL_READ.

Read-only, by construction. This looks a vendor up; it can never create, merge,
or edit one. Vendor onboarding is a separate human process with its own
approval, which is why ``NEW_VENDOR`` is a state in the machine rather than a
branch in this function.

**Two tiers, and then it stops.** A tax identifier is issued by a government and
printed on the document; a name is typed by whoever built the template. So the
tax id is tried first, then the name after normalisation, and then the answer is
no. There is no third tier that guesses, because the thing a wrong guess buys is
a payment to the wrong party - and the thing it saves is one human question.

**Ambiguity is an answer, not an error.** Two vendors whose names normalise to
the same string produce ``match_basis=none`` with both listed. Ranking them and
taking the top one is the tempting move and the wrong one: the ranking would be
a number nobody calibrated deciding who gets paid. Adjudicating that middle is a
later seat's job.

**Remittance is compared, never returned.** The caller passes the document's
``remit_to_display`` - the one field where a document is *expected* to print
payment details. Any IBAN in it is fingerprinted and compared with the
fingerprint on file, and ``remit_to_matches_master`` is the only thing that
leaves: true, false, or None when there was nothing to compare. The master's
value never enters model context because no model is anywhere near this, and
the master holds only a fingerprint in the first place. IBAN-shaped accounts
only, today: a US routing-and-account pair in the block is not recognised and
compares as None.

The master is loaded once per process and cached. It is a committed config file
of ten records, not a query: putting a network call in this step would make the
loop's routing depend on something that can be slow or down.
"""

from __future__ import annotations

import hashlib
import re
import time
from functools import lru_cache
from pathlib import Path

from pydantic import Field

from ap_agent.config import get_settings
from ap_agent.contracts.vendor import (
    MAX_VENDOR_CANDIDATES,
    VendorCandidate,
    VendorMatch,
    VendorMatchBasis,
)
from ap_agent.text import LEGAL_SUFFIXES, normalise_vendor_name
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput
from ap_agent.vendor_master import MasterVendor, load_vendor_master

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.LOCAL_READ,)
REQUIRES_IDEMPOTENCY_KEY = False


def normalise_tax_id(tax_id: str) -> str:
    """Reduce a tax identifier to its characters.

    An EIN prints as ``84-1938472`` and is filed as ``841938472``; a GSTIN is
    sometimes spaced. Neither difference is a different taxpayer.
    """
    return re.sub(r"[^0-9a-z]", "", tax_id.casefold())


class LookupVendorInput(ToolInput):
    """Input for :func:`lookup_vendor`."""

    vendor_name: str = Field(min_length=1, max_length=200)
    vendor_tax_id: str | None = Field(default=None, max_length=64)
    remit_to_display: str | None = Field(
        default=None,
        max_length=400,
        description="The remit-to block as the document printed it. Compared server-side "
        "with the master's fingerprint; never stored, logged or returned.",
    )
    address_country: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="A country read off the document, if the caller has one. Never used to "
        "*select* a vendor - only the master may say where a supplier is - and "
        "never returned. The master's country is the one that leaves this tool.",
    )
    master_path: Path | None = Field(
        default=None,
        description="Overrides the configured vendor master. For tests, and for replaying a "
        "past run against the master as it stood then.",
    )


class LookupVendorOutput(ToolOutput):
    """Output of :func:`lookup_vendor`."""

    match: VendorMatch
    latency_ms: int = Field(ge=0)


@lru_cache(maxsize=4)
def _master(path: Path | None) -> tuple[MasterVendor, ...]:
    """The vendor master, read once per path per process.

    Cached because this runs on every invoice, and because the file must not
    change mid-run: a lookup that read a different master half-way through a
    batch would make two invoices resolve differently for no recorded reason.

    Keyed by path so a caller can point at a different master without disturbing
    the cached default - which is what makes this testable without reaching into
    the cache.
    """
    return load_vendor_master(path or get_settings().vendor_master_path)


_IBAN = re.compile(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
"""An IBAN as printed: grouped in fours with single spaces, or run together."""


def remit_fingerprint(account: str) -> str:
    """SHA-256 of an account, upper-cased with all whitespace removed.

    The form the vendor master stores. ``GB29 NWBK 6016...`` and
    ``gb29nwbk6016...`` are one account and must fingerprint the same.
    """
    return hashlib.sha256(re.sub(r"\s", "", account).upper().encode()).hexdigest()


def remit_matches(display: str | None, fingerprint: str | None) -> bool | None:
    """Whether every IBAN in a remit-to block is the account on file.

    None when there is nothing to compare: no fingerprint on file, or no IBAN in
    the block (a postal address alone names no account). False if *any* printed
    IBAN differs - a block that names the real account and a second one is a
    block asking to be paid somewhere else.
    """
    if fingerprint is None or not display:
        return None
    printed = _IBAN.findall(display.upper())
    if not printed:
        return None
    return all(remit_fingerprint(account) == fingerprint for account in printed)


def _matched(
    vendor: MasterVendor, basis: VendorMatchBasis, remit_to_display: str | None
) -> VendorMatch:
    """Build a resolved match from one master record."""
    return VendorMatch(
        vendor_id=vendor.erp_id,
        vendor_name=vendor.display_name,
        country=vendor.country,
        currency=vendor.currency,
        match_basis=basis,
        remit_to_matches_master=remit_matches(remit_to_display, vendor.remit_account_sha256),
    )


def _ambiguous(vendors: list[MasterVendor]) -> VendorMatch:
    """Refuse to choose, and say what the options were."""
    return VendorMatch(
        match_basis=VendorMatchBasis.NONE,
        candidates=[
            VendorCandidate(vendor_id=v.erp_id, vendor_name=v.display_name, score=1.0)
            for v in vendors[:MAX_VENDOR_CANDIDATES]
        ],
    )


def lookup_vendor(payload: LookupVendorInput) -> LookupVendorOutput:
    """Resolve an extracted vendor name to a record in the vendor master.

    Tax identifier first, then the normalised name, then no. A name that
    normalises onto more than one master record is ``none`` with the candidates
    listed - see the module docstring for why that is not a ranking problem.

    Args:
        payload: The name and, where the document carried one, the tax id.

    Returns:
        The match, resolved or not, and how long the lookup took.

    Raises:
        VendorMasterError: The master file is missing or malformed. Loud on
            purpose: every invoice resolves against it, so a broken master is a
            configuration failure, not a per-invoice miss.
    """
    started = time.perf_counter()
    master = _master(payload.master_path)

    if payload.vendor_tax_id:
        wanted = normalise_tax_id(payload.vendor_tax_id)
        if wanted:
            hits = [v for v in master if normalise_tax_id(v.tax_id) == wanted]
            if len(hits) == 1:
                return _out(
                    _matched(hits[0], VendorMatchBasis.TAX_ID_EXACT, payload.remit_to_display),
                    started,
                )
            if len(hits) > 1:
                # Two master records sharing a tax id is a master-data problem,
                # not an invoice problem, and it is not this tool's to resolve.
                return _out(_ambiguous(hits), started)

    wanted_name = normalise_vendor_name(payload.vendor_name)
    if wanted_name:
        hits = [v for v in master if normalise_vendor_name(v.display_name) == wanted_name]
        if len(hits) == 1:
            return _out(
                _matched(hits[0], VendorMatchBasis.NAME_EXACT, payload.remit_to_display),
                started,
            )
        if len(hits) > 1:
            return _out(_ambiguous(hits), started)

    return _out(VendorMatch(match_basis=VendorMatchBasis.NONE), started)


def _out(match: VendorMatch, started: float) -> LookupVendorOutput:
    """Attach the elapsed time the audit row records."""
    return LookupVendorOutput(match=match, latency_ms=int((time.perf_counter() - started) * 1000))


__all__ = [
    "CALLER",
    "LEGAL_SUFFIXES",
    "REQUIRES_IDEMPOTENCY_KEY",
    "SIDE_EFFECTS",
    "LookupVendorInput",
    "LookupVendorOutput",
    "lookup_vendor",
    "normalise_tax_id",
    "normalise_vendor_name",
    "remit_fingerprint",
    "remit_matches",
]
