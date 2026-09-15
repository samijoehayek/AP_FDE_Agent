"""Deciding which invoice line is billing which purchase-order line.

This is the step everything else in the match depends on, and the step with no
clean answer available. Invoice lines rarely arrive in purchase-order order,
vendors re-word descriptions, a single ordered line can be billed across two
invoice lines, and freight appears on the invoice having never been ordered at
all. Every comparison the matcher makes afterwards - price, quantity, extension -
is a comparison between two lines that *this* function decided belong together.

So it is its own module, pure, and deliberately dull.

**Two keys, tried in order.** The reference the document printed, then the
description after normalisation. A printed reference is a claim the vendor makes
about which line they are billing, and it is checked rather than trusted: it has
to resolve to exactly one purchase-order line or it is not used. A description is
weaker still, which is why it comes second.

**Ambiguity never guesses.** If a key resolves to more than one purchase-order
line, that key identifies nothing and the invoice line falls through to the next
key, and then to unmatched. An unmatched line is judged against the
unmatched-charge tolerance - so the failure mode of an ambiguous description is
that a person looks at the line, which is the direction a matcher should fail in.

**One invoice line pairs to at most one purchase-order line. The reverse is not
true.** A vendor billing an ordered line across two invoice lines produces two
pairs against the same purchase-order line, and the matcher sums their quantities
before comparing to what was received. Consuming the purchase-order line on first
use would hide exactly that: two lines of 2 against a receipt of 3 would each
look fine alone and over-bill together.

Nothing here reads a price, a quantity or a tolerance. It answers "which lines go
together" and hands that to code that answers "and do they agree".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ap_agent.text import normalise_vendor_name

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ap_agent.contracts.invoice import LineItem
    from ap_agent.contracts.purchase_order import PurchaseOrderLine

__all__ = ["IndexedInvoiceLine", "LinePair", "PairedLines", "normalise_description", "pair_lines"]


def normalise_description(description: str) -> str:
    """Reduce a line description to something two documents can be compared on.

    Reuses the vendor-name normaliser from :mod:`ap_agent.text`: casefold,
    drop punctuation, collapse whitespace, strip trailing legal suffixes. The
    first three are exactly what is wanted here - ``A4 Copier Paper (box)`` and
    ``A4 copier paper box`` are one item described twice.

    The suffix stripping is the part to be aware of. It exists for vendor names,
    where ``Ltd`` and ``Limited`` are the same company, and on an item
    description it would also strip a trailing ``Co`` or ``Corp``. No catalogue
    item in this system ends in one, and sharing the implementation is worth more
    than avoiding that: two normalisers would agree until one was edited, and
    then they would disagree about which line was being billed.
    """
    return normalise_vendor_name(description)


@dataclass(frozen=True)
class IndexedInvoiceLine:
    """One invoice line and where it sat on the document.

    The index travels with the line because the match result identifies invoice
    lines by position - a description would be the one vendor-controlled string
    on the result, and position is stable for a given extraction.
    """

    index: int
    line: LineItem


@dataclass(frozen=True)
class LinePair:
    """One invoice line and the purchase-order line it is billing."""

    po_line: PurchaseOrderLine
    invoice: IndexedInvoiceLine


@dataclass(frozen=True)
class PairedLines:
    """Everything on both sides, sorted into what pairs and what does not.

    Three buckets, and each one means something different downstream:

    * ``pairs`` - compared on price, quantity and extension.
    * ``unmatched`` - on the invoice, on no purchase-order line. Judged against
      the unmatched-charge tolerance; freight is the ordinary case.
    * ``unbilled`` - ordered, not billed. **Not an exception** - a partial
      invoice against a partial delivery is normal - but recorded so a reviewer
      can see what is still outstanding.
    """

    pairs: tuple[LinePair, ...]
    unmatched: tuple[IndexedInvoiceLine, ...]
    unbilled: tuple[PurchaseOrderLine, ...]


def _unique_index(
    po_lines: Sequence[PurchaseOrderLine],
    key: Callable[[PurchaseOrderLine], str | None],
) -> dict[str, PurchaseOrderLine]:
    """Build a lookup, dropping any key that identifies more than one line.

    A key shared by two purchase-order lines identifies neither, so it is left
    out entirely rather than resolving to whichever came last. The invoice line
    then falls through to the next key and, failing that, to unmatched - where a
    person sees it.
    """
    found: dict[str, list[PurchaseOrderLine]] = {}
    for po_line in po_lines:
        value = key(po_line)
        if value:
            found.setdefault(value, []).append(po_line)
    return {value: lines[0] for value, lines in found.items() if len(lines) == 1}


def pair_lines(
    invoice_lines: Sequence[LineItem], po_lines: Sequence[PurchaseOrderLine]
) -> PairedLines:
    """Decide which invoice line bills which purchase-order line.

    Args:
        invoice_lines: ``InvoiceExtraction.line_items``, in document order.
        po_lines: The order's lines, as the ERP holds them.

    Returns:
        The pairs, the invoice lines that matched nothing, and the ordered lines
        nobody billed. Invoice lines keep their document order in both ``pairs``
        and ``unmatched``; ``unbilled`` keeps purchase-order line order.

    Never raises. An invoice this cannot pair is a match result with reasons on
    it, not an error - which is the same principle the whole matcher follows.
    """
    by_ref = _unique_index(po_lines, lambda line: line.item_ref)
    by_description = _unique_index(
        po_lines, lambda line: normalise_description(line.description or "") or None
    )

    pairs: list[LinePair] = []
    unmatched: list[IndexedInvoiceLine] = []
    paired_line_numbers: set[int] = set()

    for index, invoice_line in enumerate(invoice_lines):
        indexed = IndexedInvoiceLine(index=index, line=invoice_line)
        po_line = _match_one(invoice_line, by_ref, by_description)
        if po_line is None:
            unmatched.append(indexed)
            continue
        # The purchase-order line is not consumed. A second invoice line billing
        # it is a second pair, and the matcher sums them before comparing to what
        # was received - two lines of 2 against a receipt of 3 look fine apart
        # and over-bill together.
        pairs.append(LinePair(po_line=po_line, invoice=indexed))
        paired_line_numbers.add(po_line.line_no)

    unbilled = tuple(line for line in po_lines if line.line_no not in paired_line_numbers)
    return PairedLines(pairs=tuple(pairs), unmatched=tuple(unmatched), unbilled=unbilled)


def _match_one(
    invoice_line: LineItem,
    by_ref: dict[str, PurchaseOrderLine],
    by_description: dict[str, PurchaseOrderLine],
) -> PurchaseOrderLine | None:
    """Find the one purchase-order line this invoice line bills, or None.

    The reference first, because a vendor printing one is telling you which line
    they mean and that is better evidence than a string comparison. It is still
    only used when it resolves to exactly one order line: a reference that
    matches nothing is a claim that turned out to be wrong, and falling through
    to the description is more useful than failing on it.
    """
    printed_ref = invoice_line.po_line_ref
    if printed_ref:
        found = by_ref.get(printed_ref)
        if found is not None:
            return found

    key = normalise_description(invoice_line.description)
    return by_description.get(key) if key else None
