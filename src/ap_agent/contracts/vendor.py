"""The vendor as the *system* knows it - never as a document claims it.

``VendorRef`` is what a resolved vendor looks like to the rest of the pipeline.
Read the field list carefully for what is not there.

Bank details do not appear in this contract, in any nested contract, or in any
model context. The vendor master holds them; a server-side comparison reduces
them to a single boolean, ``remit_to_matches_master``; and that boolean is
the only thing that crosses into code the model can influence. Adding an
account number here would defeat every other control in the repository, and
``extra="forbid"`` means a well-meaning ``**payload`` will fail loudly rather
than carry one in.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from ap_agent.contracts.common import Confidence, CurrencyCode, StrictModel

MAX_VENDOR_CANDIDATES = 5
"""A shortlist a person can read. Longer than this is a search result, not a match."""


class VendorRef(StrictModel):
    """A vendor resolved against the vendor master."""

    vendor_id: str = Field(
        min_length=1,
        max_length=64,
        description="Stable identifier in the vendor master / ERP. Not read from the document.",
    )
    legal_name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(
        default=None,
        max_length=64,
        description="Tax identifier on file. Compared to the extracted one; never overwritten.",
    )
    is_active: bool = Field(
        default=True,
        description="Inactive vendors are an exception, never a straight-through payment.",
    )
    default_currency: CurrencyCode | None = None
    remit_to_matches_master: bool = Field(
        description="Computed server-side by comparing the remittance details already on file "
        "with the ones the document displayed. The comparison happens outside any "
        "model context and only this boolean is returned. False is a hard stop, "
        "not a tolerance.",
    )


class VendorMatchBasis(StrEnum):
    """How a vendor was resolved, or why it was not.

    Closed, and ordered by how much it proves. A tax identifier is issued by a
    government and printed on the document; a name is typed by whoever made the
    template. When they disagree the tax id wins, and when neither matches the
    answer is ``NONE`` - there is no "probably".
    """

    TAX_ID_EXACT = "tax_id_exact"
    NAME_EXACT = "name_exact"
    """Exact after normalisation: case, punctuation, whitespace and legal suffix."""

    NONE = "none"
    """No match, or more than one. Both route to NEW_VENDOR and a human."""


class VendorCandidate(StrictModel):
    """A near match, for a person to look at. Never auto-selected."""

    vendor_id: str = Field(min_length=1, max_length=64)
    vendor_name: str = Field(min_length=1, max_length=200)
    score: Confidence


class VendorMatch(StrictModel):
    """The outcome of looking one vendor up in the master.

    Note what the fields are *for*. ``country`` is not decoration: it is the
    input to the ambiguous-date rule, and it is the reason this lookup has to
    happen before a date can be settled. ``currency`` is the master's, to be
    compared with the document's rather than trusted from it.

    ``candidates`` is populated only when the lookup found more than one
    plausible vendor and therefore refused to choose. An ambiguous match is a
    ``NONE`` with the options listed, because a wrong vendor is a payment to the
    wrong party and a human question is cheap by comparison. Adjudicating the
    ambiguous middle is a later seat's job, not this one's.

    There is no bank-details field here, and there never will be. Remittance has
    exactly one home - the vendor master, compared server-side - and
    ``remit_to_matches_master`` is the single boolean that comparison is
    allowed to produce. ``None`` when nothing could be compared, which is a
    different and more honest answer than ``False``.
    """

    vendor_id: str | None = Field(default=None, max_length=64)
    vendor_name: str | None = Field(default=None, max_length=200)
    country: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="ISO-3166-1 alpha-2, from the master. Settles a DD/MM against a MM/DD.",
    )
    currency: CurrencyCode | None = None
    match_basis: VendorMatchBasis
    remit_to_matches_master: bool | None = Field(
        default=None,
        description="Computed server-side by fingerprinting any IBAN in the document's "
        "remit-to block and comparing it with the fingerprint on file; only the boolean "
        "is ever returned. None means no comparison was possible - nothing on file, or "
        "no account printed. False is a hard stop; None is an unanswered question, and "
        "the two must not be confused.",
    )
    candidates: list[VendorCandidate] = Field(
        default_factory=list[VendorCandidate],
        max_length=MAX_VENDOR_CANDIDATES,
        description="Listed only when the lookup refused to choose between them.",
    )

    @property
    def resolved(self) -> bool:
        """True when a single vendor was identified."""
        return self.match_basis is not VendorMatchBasis.NONE

    @model_validator(mode="after")
    def _identity_is_all_or_nothing(self) -> Self:
        """A resolved match carries a whole vendor; an unresolved one carries none of it.

        Without this a half-filled match - an id but no country, say - would
        flow into the date rule and silently leave a date open, or worse, into a
        comparison that treats ``None`` as agreement.
        """
        identity = (self.vendor_id, self.vendor_name, self.country, self.currency)
        if self.resolved and any(value is None for value in identity):
            msg = f"match_basis is {self.match_basis.value} but the vendor identity is incomplete"
            raise ValueError(msg)
        if not self.resolved and any(value is not None for value in identity):
            msg = "match_basis is none but a vendor identity is set"
            raise ValueError(msg)
        return self
