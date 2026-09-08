"""Resolve an extracted vendor name to a record in the vendor master.

Caller: code. The orchestrator calls this; no model can.
Side effects: ERP_READ, DB_READ.

Read-only, by construction. This looks a vendor up; it can never create,
merge, or edit one. Vendor onboarding is a separate human process with its
own approval, which is why ``NEW_VENDOR`` is a state in the machine rather
than a branch in this function.

``bank_details_match_on_file`` on the returned ``VendorRef`` is computed here,
server-side: the remittance details on file are compared with what the
document displayed and only the boolean is returned. The details themselves
never enter a contract or a prompt.

Not implemented: the fuzzy-name strategy, the tax-id and email-domain
tiebreakers, and the ambiguity threshold above which this must decline to
guess and hand off to a human.

"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.vendor import VendorRef
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.ERP_READ, SideEffect.DB_READ)
REQUIRES_IDEMPOTENCY_KEY = False


class LookupVendorInput(ToolInput):
    """Input for :func:`lookup_vendor`."""

    vendor_name: str = Field(min_length=1, max_length=200)
    vendor_tax_id: str | None = Field(default=None, max_length=64)
    vendor_email_domain: str | None = Field(default=None, max_length=253)
    remit_to_display: str | None = Field(
        default=None,
        description="What the document displayed. Used only for the server-side comparison "
        "that produces bank_details_match_on_file; never returned or logged.",
    )


class LookupVendorOutput(ToolOutput):
    """Output of :func:`lookup_vendor`."""

    vendor: VendorRef | None = Field(
        default=None, description="None means no confident match: route to NEW_VENDOR."
    )
    candidates: list[VendorRef] = Field(
        default_factory=list[VendorRef],
        max_length=10,
        description="Near matches, for a human to disambiguate. Ranked, never auto-selected.",
    )
    match_score: float | None = Field(default=None, ge=0.0, le=1.0)


def lookup_vendor(payload: LookupVendorInput) -> LookupVendorOutput:
    """Resolve an extracted vendor name to a record in the vendor master.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
