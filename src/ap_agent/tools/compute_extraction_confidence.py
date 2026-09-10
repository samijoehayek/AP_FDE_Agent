"""Decide how much to trust an extraction. Pure code, no model call.

Caller: code. The orchestrator calls this; no model can.
Side effects: NONE. Same inputs, same answer, every time.

A model's own confidence is not evidence - it is another thing the model said,
produced by the same process that produced the answer. This scores an extraction
on signals the model does not control, and there are exactly two that matter.

**Agreement.** Two structurally different readings - a vision model over the page
image, a cheaper text model over the PDF's own characters - either say the same
thing or they do not. Disagreement is the single strongest signal available,
because the two readings can fail in different ways.

**Grounding.** The claimed value is present in the document's text layer. This is
the stronger of the two: agreement says two models read the same thing, grounding
says the thing is actually in the file. A number that appears nowhere in the
document was invented, however confidently and however consistently.

Both are required for a field to pass. Neither alone is enough - two models can
agree on a hallucination, and a value can be grounded while the model attached it
to the wrong label.

The date rule exists because of a real failure. On invoice 51109305 the vision
model read ``09/03/2024`` as 3 September where the same vendor's other invoices
were read as day-first; the document does not disambiguate and the model guessed
differently on different days. A guess made consistently would be worse, not
better, so ambiguity is resolved from the vendor's country - and where the
country is unknown, it is escalated rather than decided.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Final

from pydantic import Field

from ap_agent.contracts.common import Confidence, StrictModel
from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.tools.base import SideEffect, ToolCaller, ToolInput, ToolOutput

CALLER = ToolCaller.CODE
SIDE_EFFECTS: tuple[SideEffect, ...] = (SideEffect.NONE,)
REQUIRES_IDEMPOTENCY_KEY = False

LOAD_BEARING_FIELDS: Final[tuple[str, ...]] = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "currency",
    "total",
)
"""The five that decide whether an invoice can proceed without a human.

Who is being paid, which document, when, in what currency, how much. Everything
else is detail that a person can correct later; getting one of these wrong pays
the wrong amount to the wrong party.
"""

SUPPORTING_FIELDS: Final[tuple[str, ...]] = ("subtotal", "tax_total")
CHECKED_FIELDS: Final[tuple[str, ...]] = LOAD_BEARING_FIELDS + SUPPORTING_FIELDS

NAME_SIMILARITY_THRESHOLD: Final = 0.9
"""Two readings of a name agree above this. Below it they disagree.

Names are the one field where exact equality is the wrong test - "Acme Ltd" and
"Acme Ltd." are the same supplier, and a check that called them different would
send every invoice to a human.
"""

MIN_SHARPNESS: Final = 800.0
"""Laplacian variance below which a page is treated as hard to read.

A starting value, not a calibrated one. It should be set from the distribution
in ``data/index.csv`` once the golden set exists; until then it is a placeholder
that errs toward asking a human.
"""

LOW_SHARPNESS_MULTIPLIER: Final = 0.6

DAY_FIRST_COUNTRIES: Final[frozenset[str]] = frozenset(
    {
        "IN",
        "GB",
        "AU",
        "NZ",
        "IE",
        "ZA",
        "EU",
        "FR",
        "DE",
        "ES",
        "IT",
        "NL",
        "BE",
        "PT",
        "SE",
        "DK",
        "FI",
        "NO",
        "PL",
        "AT",
        "CH",
        "GR",
        "CZ",
        "RO",
        "HU",
    }
)
MONTH_FIRST_COUNTRIES: Final[frozenset[str]] = frozenset({"US"})

AMBIGUOUS_DATE_REASON: Final = "ambiguous_date_unknown_locale"
RESOLVED_DATE_REASON: Final = "date_resolved_from_locale"

AMBIGUOUS_DATE_CEILING: Final = 0.4
RESOLVED_DATE_CEILING: Final = 0.9
"""A locale rule is weaker evidence than a date nobody had to interpret."""

AMBIGUOUS_SEPARATORS: Final[frozenset[str]] = frozenset({"/", "."})
"""A dash usually means ISO order; a slash or dot means nobody agreed."""

_DAY_MONTH_PATTERN: Final = re.compile(r"\b(\d{1,2})([/.\-])(\d{1,2})\2(\d{2,4})\b")
"""``09/03/2024`` - two small numbers and a year, in one of two orders."""

_YEAR_FIRST_PATTERN: Final = re.compile(r"\b(\d{4})([/.\-])(\d{1,2})\2(\d{1,2})\b")
"""``2024-03-09`` - a four-digit year leads, so the rest cannot be reordered.

Text layers are full of these and the day-month pattern does not match one, so
without this every ISO date would come back ungrounded and be escalated.
"""
_THOUSANDS = re.compile("(?<=\\d)[,\\s\\u00a0\\u202f](?=\\d)")
"""Digit-group separators: comma, any space, and the two the typography of
invoices actually uses - non-breaking and narrow no-break space."""

_NAME_NOISE = re.compile(r"[^a-z0-9 ]+")

MAX_MONTH: Final = 12
"""Above this a component cannot be a month, which resolves the order for free."""


class FieldConfidence(StrictModel):
    """What is known about one extracted field."""

    field: str = Field(min_length=1, max_length=64)
    agreed: bool | None = Field(
        description="Did the two readings match? None when there was only one reading."
    )
    grounded: bool = Field(description="Does the value appear in the document's text layer?")
    score: Confidence
    reason: str = Field(max_length=200, description="Why the score is what it is.")


class ExtractionConfidence(StrictModel):
    """Whether this extraction can proceed without a person looking at it."""

    fields: list[FieldConfidence] = Field(default_factory=list[FieldConfidence])
    auto_ok: bool = Field(
        description="True only when every load-bearing field both agreed and was grounded."
    )
    needs_human: list[str] = Field(
        default_factory=list[str],
        description="Field names and blocking reasons, in the order found.",
    )
    resolved_invoice_date: date | None = Field(
        default=None,
        description="The date after resolving an ambiguous rendering from the vendor's country. "
        "May differ from the extracted date - that is the point.",
    )

    def by_field(self) -> dict[str, FieldConfidence]:
        """Return the per-field entries keyed by field name."""
        return {entry.field: entry for entry in self.fields}


class ComputeExtractionConfidenceInput(ToolInput):
    """Two readings and the document they came from."""

    primary: InvoiceExtraction
    secondary: InvoiceExtraction | None = Field(
        default=None, description="The text-layer reading. None when the document had no text."
    )
    raw_text: str = Field(default="", description="The document's text layer. Empty means a scan.")
    vendor_country: str | None = Field(
        default=None,
        max_length=2,
        description="ISO-3166-1 alpha-2. Decides day-first from month-first. Unknown escalates.",
    )
    min_sharpness: float | None = Field(default=None, ge=0)


class ComputeExtractionConfidenceOutput(ToolOutput):
    """The verdict."""

    confidence: ExtractionConfidence


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def _as_number(value: object) -> Decimal | None:
    """Parse a value as a number, ignoring separators. None if it is not one."""
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        return None
    try:
        return Decimal(_THOUSANDS.sub("", value.strip()))
    except InvalidOperation:
        return None


def _normalise_name(value: str) -> str:
    """Casefold, drop punctuation, collapse whitespace."""
    return " ".join(_NAME_NOISE.sub(" ", value.casefold()).split())


def name_similarity(left: str, right: str) -> float:
    """Return 0..1 similarity between two names after normalising both."""
    return SequenceMatcher(None, _normalise_name(left), _normalise_name(right)).ratio()


def _numbers_agree(left: object, right: object) -> bool:
    """Compare as numbers, so 1676976 and 1,676,976.00 are the same value."""
    a, b = _as_number(left), _as_number(right)
    return a is not None and b is not None and a == b


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------


def _grounded_number(value: Decimal, raw_text: str) -> bool:
    """Is this amount present in the text, under any grouping convention?

    The text is searched twice: as written, and with digit-group separators
    removed. Stripping them handles Western and Indian grouping alike without
    the check having to know which one the document used.
    """
    stripped_text = _THOUSANDS.sub("", raw_text)
    plain = _THOUSANDS.sub("", str(value))
    renderings = {plain, str(value)}

    normalised = value.normalize()
    renderings.add(str(normalised))
    if normalised == normalised.to_integral_value():
        renderings.add(str(normalised.to_integral_value()))
    renderings.add(f"{value:,}")

    return any(r and (r in raw_text or r in stripped_text) for r in renderings)


def _grounded_text(value: str, raw_text: str) -> bool:
    """Is this string present, ignoring case and punctuation?"""
    if not value:
        return False
    return _normalise_name(value) in _normalise_name(raw_text)


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


class RawDate(StrictModel):
    """A date as the document rendered it, before anyone decided what it means."""

    text: str
    first: int
    second: int
    year: int
    separator: str
    year_first: bool = False
    """True for ``2024-03-09``, where the leading year fixes the rest of the order."""

    @property
    def is_ambiguous(self) -> bool:
        """True when either component could be the month and nothing settles which."""
        if self.year_first:
            return False
        return (
            self.separator in AMBIGUOUS_SEPARATORS
            and self.first <= MAX_MONTH
            and self.second <= MAX_MONTH
        )

    def resolve(self, *, day_first: bool) -> date | None:
        """Return the date under the chosen reading order, or None if impossible."""
        if self.year_first:
            month, day = self.first, self.second
        else:
            day, month = (self.first, self.second) if day_first else (self.second, self.first)
        year = self.year + 2000 if self.year < 100 else self.year  # noqa: PLR2004
        try:
            return date(year, month, day)
        except ValueError:
            return None


def find_raw_date(raw_text: str, extracted: date) -> RawDate | None:
    """Find the rendering in the text that the extracted date came from.

    Matches on either reading order, because the whole question is which order
    was meant - insisting on one would beg it.
    """
    for candidate in _candidates(raw_text):
        if extracted in (
            candidate.resolve(day_first=True),
            candidate.resolve(day_first=False),
        ):
            return candidate
    return None


def _candidates(raw_text: str) -> list[RawDate]:
    """Every date-shaped run in the text, in both renderings."""
    found = [
        RawDate(
            text=match.group(0),
            first=int(match.group(3)),
            second=int(match.group(4)),
            year=int(match.group(1)),
            separator=match.group(2),
            year_first=True,
        )
        for match in _YEAR_FIRST_PATTERN.finditer(raw_text)
    ]
    consumed = {candidate.text for candidate in found}
    found.extend(
        RawDate(
            text=match.group(0),
            first=int(match.group(1)),
            second=int(match.group(3)),
            year=int(match.group(4)),
            separator=match.group(2),
        )
        # A year-first date contains a day-month-shaped tail (``24-03-09``),
        # so anything already claimed above is not offered again.
        for match in _DAY_MONTH_PATTERN.finditer(raw_text)
        if not any(match.group(0) in text for text in consumed)
    )
    return found


def _country_reading(vendor_country: str | None) -> bool | None:
    """Return True for day-first, False for month-first, None for unknown."""
    if not vendor_country:
        return None
    code = vendor_country.strip().upper()
    if code in DAY_FIRST_COUNTRIES:
        return True
    if code in MONTH_FIRST_COUNTRIES:
        return False
    return None


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

_SCORES: Final[dict[tuple[bool | None, bool], float]] = {
    (True, True): 1.0,
    (True, False): 0.5,
    (None, True): 0.6,
    (None, False): 0.3,
    (False, True): 0.2,
    (False, False): 0.1,
}
"""Agreement and grounding, combined.

Disagreement scores lowest even when grounded: if the two readings differ, at
least one of them is wrong about a field that decides money, and which one is
not something this function can know.
"""


def _reason(agreed: bool | None, grounded: bool, has_text: bool) -> str:
    if agreed is False:
        return "readings_disagree"
    if not grounded:
        return "not_in_text_layer" if has_text else "no_text_layer"
    if agreed is None:
        return "single_read"
    return "agreed_and_grounded"


def _field_values(extraction: InvoiceExtraction) -> dict[str, object]:
    return {name: getattr(extraction, name) for name in CHECKED_FIELDS}


def _assess(
    field: str,
    primary_value: object,
    secondary_value: object | None,
    raw_text: str,
    *,
    has_secondary: bool,
) -> tuple[bool | None, bool]:
    """Return ``(agreed, grounded)`` for one field."""
    if not has_secondary:
        agreed = None
    elif field == "invoice_date":
        agreed = _dates_agree(primary_value, secondary_value, raw_text)
    elif field == "vendor_name":
        agreed = (
            isinstance(primary_value, str)
            and isinstance(secondary_value, str)
            and name_similarity(primary_value, secondary_value) >= NAME_SIMILARITY_THRESHOLD
        )
    elif field in ("subtotal", "tax_total", "total"):
        agreed = _numbers_agree(primary_value, secondary_value)
    else:
        agreed = str(primary_value) == str(secondary_value)

    if not raw_text:
        grounded = False
    elif isinstance(primary_value, Decimal):
        grounded = _grounded_number(primary_value, raw_text)
    elif isinstance(primary_value, date):
        grounded = find_raw_date(raw_text, primary_value) is not None
    else:
        grounded = _grounded_text(str(primary_value), raw_text)

    return agreed, grounded


def _dates_agree(primary_value: object, secondary_value: object, raw_text: str) -> bool:
    """Did the two readings see the same characters on the page?

    Not the same calendar date - the same rendering. Two models given
    ``09/03/2024`` may order it differently, and that is a fact about the
    document being ambiguous, not about either model misreading it. Conflating
    the two would report a disagreement on every ambiguous date and hide the
    real ones. What the characters *mean* is settled separately, by
    :func:`_resolve_date`, which has the vendor's country to settle it with.
    """
    if not isinstance(primary_value, date) or not isinstance(secondary_value, date):
        return False
    if primary_value == secondary_value:
        return True
    if not raw_text:
        return False
    left = find_raw_date(raw_text, primary_value)
    right = find_raw_date(raw_text, secondary_value)
    return left is not None and right is not None and left.text == right.text


def compute_extraction_confidence(
    payload: ComputeExtractionConfidenceInput,
) -> ComputeExtractionConfidenceOutput:
    """Score an extraction on agreement and grounding, and decide who sees it next.

    Args:
        payload: The two readings, the document text, the vendor's country and
            the page sharpness.

    Returns:
        A per-field verdict, whether the invoice may proceed unattended, and
        the invoice date after resolving an ambiguous rendering.
    """
    primary = payload.primary
    secondary = payload.secondary
    raw_text = payload.raw_text
    has_secondary = secondary is not None
    has_text = bool(raw_text)

    low_sharpness = payload.min_sharpness is not None and payload.min_sharpness < MIN_SHARPNESS

    primary_values = _field_values(primary)
    secondary_values = _field_values(secondary) if secondary else {}

    resolved_date, date_note, date_blocks = _resolve_date(primary, raw_text, payload.vendor_country)

    entries: list[FieldConfidence] = []
    needs_human: list[str] = []

    for field in CHECKED_FIELDS:
        agreed, grounded = _assess(
            field,
            primary_values[field],
            secondary_values.get(field),
            raw_text,
            has_secondary=has_secondary,
        )
        reason = _reason(agreed, grounded, has_text)
        score = _SCORES[(agreed, grounded)]

        if field == "invoice_date" and date_note:
            reason = date_note
            score = min(score, AMBIGUOUS_DATE_CEILING if date_blocks else RESOLVED_DATE_CEILING)

        if low_sharpness:
            score *= LOW_SHARPNESS_MULTIPLIER
            reason = f"{reason}, low_sharpness"

        entries.append(
            FieldConfidence(
                field=field, agreed=agreed, grounded=grounded, score=score, reason=reason
            )
        )

        # A single read is one fact about the run, not seven facts about
        # fields; it is reported once below rather than against every name.
        blocked = agreed is False or not grounded
        if field in LOAD_BEARING_FIELDS and blocked:
            needs_human.append(field)

    if not has_secondary:
        needs_human.append("single_read")
    if date_blocks:
        needs_human.append(date_note)
    if low_sharpness:
        needs_human.append("low_sharpness")

    auto_ok = not needs_human

    return ComputeExtractionConfidenceOutput(
        confidence=ExtractionConfidence(
            fields=entries,
            auto_ok=auto_ok,
            needs_human=needs_human,
            resolved_invoice_date=resolved_date,
        )
    )


def _resolve_date(
    primary: InvoiceExtraction, raw_text: str, vendor_country: str | None
) -> tuple[date | None, str, bool]:
    """Return ``(resolved_date, note, blocks)`` for the invoice date.

    An unambiguous rendering resolves to itself and says nothing. An ambiguous
    one resolves from the vendor's country - and says so, because a date that a
    locale rule reinterpreted is not the same kind of fact as one the document
    stated plainly, and six months later the audit trail should show which it
    was. Where the country is unknown it does not resolve at all: a consistent
    guess about a date the document never specified is worse than an escalation,
    because it is wrong silently.
    """
    raw = find_raw_date(raw_text, primary.invoice_date) if raw_text else None
    if raw is None or not raw.is_ambiguous:
        return primary.invoice_date, "", False

    reading = _country_reading(vendor_country)
    if reading is None:
        return None, AMBIGUOUS_DATE_REASON, True

    resolved = raw.resolve(day_first=reading)
    if resolved is None:
        return primary.invoice_date, "", False
    return resolved, RESOLVED_DATE_REASON, False


__all__ = [
    "AMBIGUOUS_DATE_REASON",
    "CHECKED_FIELDS",
    "DAY_FIRST_COUNTRIES",
    "LOAD_BEARING_FIELDS",
    "MIN_SHARPNESS",
    "NAME_SIMILARITY_THRESHOLD",
    "RESOLVED_DATE_REASON",
    "ComputeExtractionConfidenceInput",
    "ComputeExtractionConfidenceOutput",
    "ExtractionConfidence",
    "FieldConfidence",
    "RawDate",
    "compute_extraction_confidence",
    "find_raw_date",
    "name_similarity",
]
