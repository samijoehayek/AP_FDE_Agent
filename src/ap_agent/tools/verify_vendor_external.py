"""Check a vendor against an external registry.

Caller: code. The orchestrator calls this; no model can.
Side effects: EXTERNAL_READ.

Read-only against a third party (VAT/EIN registry, sanctions list, company
register). Used when a vendor is new or when identity signals disagree.

The result is evidence for a human. It never auto-approves anything, and a
failure to reach the registry is an unknown, not a pass.

Not implemented: the registry clients, the cache with its TTL, and the
rate limiter.

"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.EXTERNAL_READ,)
REQUIRES_IDEMPOTENCY_KEY = False


class VerifyVendorExternalInput(ToolInput):
    """Input for :func:`verify_vendor_external`."""

    legal_name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=64)
    country: str = Field(min_length=2, max_length=2, description="ISO-3166-1 alpha-2.")


class VerifyVendorExternalOutput(ToolOutput):
    """Output of :func:`verify_vendor_external`."""

    registry_name: str | None = Field(default=None, max_length=200)
    tax_id_valid: bool | None = Field(
        default=None, description="None means the registry could not be reached - not a pass."
    )
    name_matches: bool | None = None
    sanctions_hit: bool | None = None
    checked_at: datetime | None = None
    source: str | None = Field(default=None, max_length=120)


def verify_vendor_external(payload: VerifyVendorExternalInput) -> VerifyVendorExternalOutput:
    """Check a vendor against an external registry.

    Raises:
        NotImplementedError: Written by hand in a later session.
    """
    raise NotImplementedError
