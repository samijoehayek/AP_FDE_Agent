"""What the duplicate check compares, and how it says which rule fired.

Responsible for the shape of one ``dedup_index`` entry and the closed list of
reasons two invoices can be held as possible duplicates. The rule itself is in
``tools/find_duplicates.py``; storage is in ``db/store.py``.

An entry carries no prose. The invoice number is *normalised* before it gets
here, so the comparison is between identifiers this system computed, not
strings a vendor typed.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import Field

from ap_agent.contracts.common import CurrencyCode, Money, StrictModel

__all__ = ["DedupEntry", "DuplicateRule"]


class DuplicateRule(StrEnum):
    """Why two invoices were held as possible duplicates. Closed."""

    EXACT_NUMBER = "exact_number"
    """Same vendor, same normalised number. The database refuses a second one anyway."""
    NEAR_NUMBER = "near_number"
    """Same vendor, a number this similar, an amount this close, a date this near."""
    SAME_AMOUNT = "same_amount"
    """Same vendor, the same amount, within the window."""


class DedupEntry(StrictModel):
    """One invoice as the duplicate check sees it."""

    invoice_id: str = Field(min_length=1, max_length=26)
    vendor_id: str = Field(min_length=1, max_length=64)
    normalised_invoice_number: str = Field(min_length=1, max_length=64)
    amount: Money
    currency: CurrencyCode
    invoice_date: date
