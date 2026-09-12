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
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Final

from pydantic import Field

from ap_agent.contracts.common import (
    AmbiguousNumber,
    Confidence,
    StrictModel,
    normalise_decimal_text,
)
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


class DateVerdict(StrEnum):
    """How much interpretation the invoice date needed."""

    UNAMBIGUOUS = "unambiguous"
    """The page states one date and only one reading of it is possible."""

    RESOLVED = "resolved"
    """The page was ambiguous and something outside the page settled it."""

    AMBIGUOUS = "ambiguous"
    """Two readings are still live. Not a failure - an open question."""


class DateResolutionReason(StrEnum):
    """What settled an ambiguous date. Recorded so the trail shows which rule ran."""

    LOCALE = "date_resolved_from_locale"
    """The vendor's country was supplied to this tool."""

    RECEIPT_WINDOW = "date_resolved_from_receipt_window"
    """Only one candidate could have been received when the document was."""

    VENDOR_LOCALE = "date_resolved_from_vendor_locale"
    """The vendor master's country, known only once the vendor is resolved."""


AGREED_AND_GROUNDED: Final = "agreed_and_grounded"
"""The only reason that means nothing is wrong. Everything else blocks or qualifies."""

AMBIGUOUS_DATE_REASON: Final = "ambiguous_date_unknown_locale"
RESOLVED_DATE_REASON: Final = DateResolutionReason.LOCALE.value

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

MONTH_NAMES: Final[dict[str, int]] = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
"""Month names and the abbreviations invoices actually print."""

_DAY_MONTH_NAME_PATTERN: Final = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
"""``09 Mar 2024`` - a named month, so nothing can be reordered."""

_MONTH_NAME_DAY_PATTERN: Final = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
"""``Mar 9, 2024`` - the same date the other way round, equally unambiguous.

Both patterns exist because a named month is the *unambiguous* way to print a
date, which is exactly why a generated fixture uses one - and a check that could
not ground the unambiguous rendering would escalate every clean document for a
date that is plainly on the page.
"""
_NAME_NOISE = re.compile(r"[^a-z0-9 ]+")

MAX_MONTH: Final = 12
"""Above this a component cannot be a month, which resolves the order for free."""

MIN_LIVE_READINGS: Final = 2
"""Fewer than this and there is nothing to be ambiguous between."""


class FieldConfidence(StrictModel):
    """What is known about one extracted field."""

    field: str = Field(min_length=1, max_length=64)
    agreed: bool | None = Field(
        description="Did the two readings match? None when there was only one reading."
    )
    grounded: bool = Field(description="Does the value appear in the document's text layer?")
    score: Confidence
    reason: str = Field(max_length=200, description="Why the score is what it is.")


class HumanReviewItem(StrictModel):
    """One thing a person has to look at, and where to look.

    A bare reason string is not enough to build a queue on. "not_in_text_layer"
    tells a reviewer what went wrong but not which of seven fields to open the
    document for, and a queue that cannot say that makes every item a full
    re-read.
    """

    field: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=200)


class ExtractionConfidence(StrictModel):
    """Whether this extraction can proceed without a person looking at it."""

    fields: list[FieldConfidence] = Field(default_factory=list[FieldConfidence])
    auto_ok: bool = Field(
        description="True if and only if every load-bearing field both agreed and was grounded."
    )
    needs_human: list[HumanReviewItem] = Field(
        default_factory=list[HumanReviewItem],
        description="Which field to look at and why, in the order found. Empty iff auto_ok.",
    )

    date_verdict: DateVerdict = Field(
        default=DateVerdict.UNAMBIGUOUS,
        description="How much interpretation the invoice date needed.",
    )
    resolved_invoice_date: date | None = Field(
        default=None,
        description="The effective date. None only when the verdict is ambiguous - the page "
        "supports two readings and nothing here can choose between them.",
    )
    date_candidates: list[date] = Field(
        default_factory=list[date],
        max_length=2,
        description="Both live readings, ascending. Empty unless the verdict is ambiguous. "
        "Carried forward so a later state can settle it without re-reading the document.",
    )
    date_resolution_reason: DateResolutionReason | None = Field(
        default=None, description="What settled it. None when nothing had to."
    )
    date_raw_text: str | None = Field(
        default=None,
        max_length=32,
        description="The date exactly as the page renders it, e.g. '09/03/2024'. Empty unless "
        "the verdict is ambiguous. Carried because the two candidates alone cannot say "
        "which of them was the day-first reading - {2024-03-09, 2024-09-03} is the same "
        "pair whether the page said 09/03 or 03/09 - so a later locale rule needs the "
        "digits. Computed by code from the text layer, never asked of a model.",
    )

    def by_field(self) -> dict[str, FieldConfidence]:
        """Return the per-field entries keyed by field name."""
        return {entry.field: entry for entry in self.fields}

    def blocking_fields(self) -> list[str]:
        """The field names a person must look at, in the order found."""
        return [item.field for item in self.needs_human]


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
    """Parse a value as a number, whatever grouping it was printed with.

    Delegates to the contract's own normaliser rather than keeping a second copy
    of the rule. Two implementations of "what does this comma mean" would agree
    until one was edited, and then they would disagree about money.

    Returns None where the contract refuses, because this is a *scoring*
    function: an unreadable value scores as ungrounded and the field goes to a
    person, which is the same destination the contract's refusal reaches by a
    different road.
    """
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str):
        return None
    try:
        return normalise_decimal_text(value)
    except AmbiguousNumber:
        return None


_DIGIT_GROUPING = re.compile(r"(?<=\d)[,\u0020\u00a0\u202f\u2009](?=\d)")
"""Separators *between digits*, for searching text rather than parsing a value.

Distinct from :func:`normalise_decimal_text`, and deliberately so: that function
refuses when a rendering is ambiguous, which is right when the answer becomes a
number someone pays. This one only has to make two strings comparable, so it
strips and moves on. Note it leaves ``.`` alone - removing it would make
``1.234`` and ``1234`` look alike in a body of text.
"""


def _degrouped(text: str) -> str:
    """Text with digit-group separators removed, for substring comparison."""
    return _DIGIT_GROUPING.sub("", text)


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
    stripped_text = _degrouped(raw_text)
    plain = _degrouped(str(value))
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

    month_named: bool = False
    """True for ``09 Mar 2024``. ``first`` is the day and ``second`` the month.

    A spelled-out month cannot be read two ways, so no locale rule applies and
    ``day_first`` is ignored when resolving.
    """

    @property
    def is_ambiguous(self) -> bool:
        """True when either component could be the month and nothing settles which."""
        if self.year_first or self.month_named:
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
        elif self.month_named:
            day, month = self.first, self.second
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


def _named_month_candidates(raw_text: str) -> list[RawDate]:
    """Every date printed with a spelled-out month, in either order."""
    found: list[RawDate] = []
    for match in _DAY_MONTH_NAME_PATTERN.finditer(raw_text):
        month = MONTH_NAMES.get(match.group(2).casefold())
        if month is not None:
            found.append(
                RawDate(
                    text=match.group(0),
                    first=int(match.group(1)),
                    second=month,
                    year=int(match.group(3)),
                    # Empty rather than a space: StrictModel strips whitespace,
                    # and is_ambiguous never reads it on a named-month date.
                    separator="",
                    month_named=True,
                )
            )
    for match in _MONTH_NAME_DAY_PATTERN.finditer(raw_text):
        month = MONTH_NAMES.get(match.group(1).casefold())
        if month is not None:
            found.append(
                RawDate(
                    text=match.group(0),
                    first=int(match.group(2)),
                    second=month,
                    year=int(match.group(3)),
                    # Empty rather than a space: StrictModel strips whitespace,
                    # and is_ambiguous never reads it on a named-month date.
                    separator="",
                    month_named=True,
                )
            )
    return found


def _candidates(raw_text: str) -> list[RawDate]:
    """Every date-shaped run in the text, in every rendering."""
    found = _named_month_candidates(raw_text)
    found.extend(
        RawDate(
            text=match.group(0),
            first=int(match.group(3)),
            second=int(match.group(4)),
            year=int(match.group(1)),
            separator=match.group(2),
            year_first=True,
        )
        for match in _YEAR_FIRST_PATTERN.finditer(raw_text)
    )
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


def parse_raw_date(text: str) -> RawDate | None:
    """Parse one date-shaped string, e.g. ``"09/03/2024"``. None if it is not one.

    Public because a later state re-reads a carried-forward rendering to settle
    it, and re-implementing this parser there is how two parsers drift apart.
    """
    found = _candidates(text)
    return found[0] if found else None


def country_reads_day_first(vendor_country: str | None) -> bool | None:
    """True for day-first, False for month-first, None when the country is unknown.

    Public for the same reason as :func:`parse_raw_date`: the locale table is one
    table, and a second copy of it in the loop would answer differently the first
    time either was edited.
    """
    return _country_reading(vendor_country)


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
    return AGREED_AND_GROUNDED


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


class DateAssessment(StrictModel):
    """What the invoice date is, and how much interpretation that took."""

    verdict: DateVerdict
    resolved: date | None = None
    candidates: list[date] = Field(default_factory=list[date], max_length=2)
    reason: DateResolutionReason | None = None
    raw_text: str | None = Field(default=None, max_length=32)
    """The rendering on the page, kept only while the date is still open."""

    note: str = Field(default="", max_length=200)
    """The per-field reason string, empty when the date needed no interpretation."""

    @property
    def ceiling(self) -> float:
        """The most this field may score. A rule's answer is weaker than the page's."""
        if self.verdict is DateVerdict.AMBIGUOUS:
            return AMBIGUOUS_DATE_CEILING
        if self.verdict is DateVerdict.RESOLVED:
            return RESOLVED_DATE_CEILING
        return 1.0


def assess_date(
    primary: InvoiceExtraction, raw_text: str, vendor_country: str | None
) -> DateAssessment:
    """Decide what the invoice date is, and say how sure that is.

    An unambiguous rendering resolves to itself and says nothing. An ambiguous
    one resolves from the vendor's country - and says so, because a date a rule
    reinterpreted is not the same kind of fact as one the document stated
    plainly, and six months later the trail should show which it was.

    Where the country is unknown, the date does **not** resolve and both
    readings are carried forward. That is deliberately not a failure: the state
    that knows the vendor's country runs two states after this one, and failing
    here would send every slash-dated invoice to a person for a question the
    pipeline can answer itself a moment later. A consistent guess would be
    worse than either - silently and reproducibly wrong.
    """
    raw = find_raw_date(raw_text, primary.invoice_date) if raw_text else None
    if raw is None or not raw.is_ambiguous:
        return DateAssessment(verdict=DateVerdict.UNAMBIGUOUS, resolved=primary.invoice_date)

    readings = sorted(
        {
            reading
            for reading in (raw.resolve(day_first=True), raw.resolve(day_first=False))
            if reading is not None
        }
    )
    if len(readings) < MIN_LIVE_READINGS:
        # 05/05/2024 reads the same both ways, and 31/02/2024 reads only one.
        # Either way there is nothing left to choose between.
        return DateAssessment(
            verdict=DateVerdict.UNAMBIGUOUS,
            resolved=readings[0] if readings else primary.invoice_date,
        )

    day_first = _country_reading(vendor_country)
    if day_first is None:
        return DateAssessment(
            verdict=DateVerdict.AMBIGUOUS,
            candidates=readings,
            raw_text=raw.text,
            note=AMBIGUOUS_DATE_REASON,
        )

    resolved = raw.resolve(day_first=day_first)
    if resolved is None:
        return DateAssessment(verdict=DateVerdict.UNAMBIGUOUS, resolved=primary.invoice_date)
    return DateAssessment(
        verdict=DateVerdict.RESOLVED,
        resolved=resolved,
        reason=DateResolutionReason.LOCALE,
        note=RESOLVED_DATE_REASON,
    )


def compute_extraction_confidence(
    payload: ComputeExtractionConfidenceInput,
) -> ComputeExtractionConfidenceOutput:
    """Score an extraction on agreement and grounding, and decide who sees it next.

    ``auto_ok`` is true if and only if every load-bearing field both agreed and
    was grounded. Nothing else can block it - not a blurry page, not an
    ambiguous date. Those lower the score and are recorded, because they are
    real observations, but neither is evidence that a value is *wrong*, and a
    gate that stops on everything suspicious teaches reviewers to click through.

    Args:
        payload: The two readings, the document text, the vendor's country and
            the page sharpness.

    Returns:
        A per-field verdict, whether the invoice may proceed unattended, and
        what is known about the invoice date.
    """
    primary = payload.primary
    secondary = payload.secondary
    raw_text = payload.raw_text
    has_secondary = secondary is not None
    has_text = bool(raw_text)

    low_sharpness = payload.min_sharpness is not None and payload.min_sharpness < MIN_SHARPNESS

    primary_values = _field_values(primary)
    secondary_values = _field_values(secondary) if secondary else {}

    date_assessment = assess_date(primary, raw_text, payload.vendor_country)

    entries: list[FieldConfidence] = []
    needs_human: list[HumanReviewItem] = []

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

        if field == "invoice_date" and date_assessment.note:
            # The date note replaces a clean reason and *qualifies* a blocking
            # one. A field flagged for single_read that then reads
            # "date_resolved_from_locale" would send a reviewer looking for a
            # date problem that is not the reason it was flagged.
            reason = (
                date_assessment.note
                if reason == AGREED_AND_GROUNDED
                else f"{reason}, {date_assessment.note}"
            )
            score = min(score, date_assessment.ceiling)

        if low_sharpness:
            score *= LOW_SHARPNESS_MULTIPLIER
            reason = f"{reason}, low_sharpness"

        entries.append(
            FieldConfidence(
                field=field, agreed=agreed, grounded=grounded, score=score, reason=reason
            )
        )

        if field in LOAD_BEARING_FIELDS and not (agreed and grounded):
            needs_human.append(HumanReviewItem(field=field, reason=reason))

    return ComputeExtractionConfidenceOutput(
        confidence=ExtractionConfidence(
            fields=entries,
            auto_ok=not needs_human,
            needs_human=needs_human,
            date_verdict=date_assessment.verdict,
            resolved_invoice_date=date_assessment.resolved,
            date_candidates=date_assessment.candidates,
            date_resolution_reason=date_assessment.reason,
            date_raw_text=date_assessment.raw_text,
        )
    )


__all__ = [
    "AGREED_AND_GROUNDED",
    "AMBIGUOUS_DATE_REASON",
    "CHECKED_FIELDS",
    "DAY_FIRST_COUNTRIES",
    "LOAD_BEARING_FIELDS",
    "MIN_SHARPNESS",
    "MONTH_NAMES",
    "NAME_SIMILARITY_THRESHOLD",
    "RESOLVED_DATE_REASON",
    "ComputeExtractionConfidenceInput",
    "ComputeExtractionConfidenceOutput",
    "DateAssessment",
    "DateResolutionReason",
    "DateVerdict",
    "ExtractionConfidence",
    "FieldConfidence",
    "HumanReviewItem",
    "RawDate",
    "assess_date",
    "compute_extraction_confidence",
    "country_reads_day_first",
    "find_raw_date",
    "name_similarity",
    "parse_raw_date",
]
