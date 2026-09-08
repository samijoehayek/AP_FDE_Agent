"""The vendor as the *system* knows it - never as a document claims it.

``VendorRef`` is what a resolved vendor looks like to the rest of the pipeline.
Read the field list carefully for what is not there.

Bank details do not appear in this contract, in any nested contract, or in any
model context. The vendor master holds them; a server-side comparison reduces
them to a single boolean, ``bank_details_match_on_file``; and that boolean is
the only thing that crosses into code the model can influence. Adding an
account number here would defeat every other control in the repository, and
``extra="forbid"`` means a well-meaning ``**payload`` will fail loudly rather
than carry one in.
"""

from __future__ import annotations

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, StrictModel


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
    bank_details_match_on_file: bool = Field(
        description="Computed server-side by comparing the remittance details already on file "
        "with the ones the document displayed. The comparison happens outside any "
        "model context and only this boolean is returned. False is a hard stop, "
        "not a tolerance.",
    )
