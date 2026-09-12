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

Nothing about remittance is read here. ``bank_details_match_on_file`` is
``None`` because the master holds no remittance details - a comparison that did
not happen, which is a different answer from one that failed.

The master is loaded once per process and cached. It is a committed config file
of ten records, not a query: putting a network call in this step would make the
loop's routing depend on something that can be slow or down.
"""

from __future__ import annotations

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
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput
from ap_agent.vendor_master import MasterVendor, load_vendor_master

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.LOCAL_READ,)
REQUIRES_IDEMPOTENCY_KEY = False

LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "ltd",
        "limited",
        "pvt",
        "private",
        "inc",
        "incorporated",
        "llc",
        "llp",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "bv",
        "sa",
        "sarl",
        "ag",
        "pty",
    }
)
"""Tokens that say how a company is incorporated, not which company it is.

Stripped from the *end* only. "Acme Ltd" and "ACME Limited" are one supplier
with two renderings, and an AP function that treated them as two would pay the
same invoice twice under different vendor ids. Stripping them anywhere in the
string would be wrong: "Limited Editions Ltd" is a name.
"""

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise_vendor_name(name: str) -> str:
    """Reduce a printed vendor name to what identifies the company.

    Case, punctuation, spacing and legal suffix are rendering, not identity. A
    supplier prints "ACME Ltd." on one template and "Acme Limited" on the next,
    and both are the same party being paid.

    Pure, and its own function rather than three lines inside the lookup,
    because the cost of getting it wrong is asymmetric: too aggressive and two
    real suppliers collapse into one, too timid and the same supplier is
    onboarded twice. That trade-off deserves to be argued with in its own tests.

    Args:
        name: The name as printed on the document or held in the master.

    Returns:
        The normalised form: casefolded, punctuation removed, whitespace
        collapsed, trailing legal suffixes dropped. May be empty if the name was
        nothing but punctuation and suffixes, and an empty result never matches.
    """
    folded = _PUNCTUATION.sub(" ", name.casefold())
    tokens = _WHITESPACE.sub(" ", folded).strip().split(" ")
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


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


def _matched(vendor: MasterVendor, basis: VendorMatchBasis) -> VendorMatch:
    """Build a resolved match from one master record."""
    return VendorMatch(
        vendor_id=vendor.erp_id,
        vendor_name=vendor.display_name,
        country=vendor.country,
        currency=vendor.currency,
        match_basis=basis,
        # The master holds no remittance details, so no comparison happened.
        bank_details_match_on_file=None,
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
                return _out(_matched(hits[0], VendorMatchBasis.TAX_ID_EXACT), started)
            if len(hits) > 1:
                # Two master records sharing a tax id is a master-data problem,
                # not an invoice problem, and it is not this tool's to resolve.
                return _out(_ambiguous(hits), started)

    wanted_name = normalise_vendor_name(payload.vendor_name)
    if wanted_name:
        hits = [v for v in master if normalise_vendor_name(v.display_name) == wanted_name]
        if len(hits) == 1:
            return _out(_matched(hits[0], VendorMatchBasis.NAME_EXACT), started)
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
]
