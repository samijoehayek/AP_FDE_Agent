"""What the confidence check must get right, and one bug it exists to catch.

No API calls anywhere in this file. The tool is pure code, so every case here is
a fixture and an assertion - which is the point of having built it that way.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum, auto
from typing import Final

import pytest

from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.tools.compute_extraction_confidence import (
    AMBIGUOUS_DATE_REASON,
    LOAD_BEARING_FIELDS,
    MIN_SHARPNESS,
    RESOLVED_DATE_REASON,
    ComputeExtractionConfidenceInput,
    ExtractionConfidence,
    compute_extraction_confidence,
    find_raw_date,
    name_similarity,
)

# --- fixtures ---------------------------------------------------------------

RAW_51109305 = """Sunrise Traders Pvt Ltd
21 Nehru Road, Bengaluru 560001
GSTIN 29AABCS1429B1ZQ

Invoice No: 51109305
Date: 09/03/2024

Subtotal        1,42,000.00
GST 18%            25,560.00
Total INR       1,67,560.00
"""
"""Invoice 51109305, reduced to the parts the check reads.

The date is the whole reason this file exists. See the module docstring of the
tool: the vision model read ``09/03/2024`` as 3 September, the vendor is Indian,
and the correct reading is 9 March. Note the Indian digit grouping too - the
grounding check has to find ``1,67,560.00`` without knowing that convention.
"""


def extraction(**overrides: object) -> InvoiceExtraction:
    """An extraction of invoice 51109305, as the vision model actually read it."""
    fields: dict[str, object] = {
        "vendor_name": "Sunrise Traders Pvt Ltd",
        "invoice_number": "51109305",
        "invoice_date": date(2024, 9, 3),
        "currency": "INR",
        "subtotal": Decimal("142000.00"),
        "tax_total": Decimal("25560.00"),
        "total": Decimal("167560.00"),
    }
    return InvoiceExtraction.model_validate(fields | overrides)


class _Mirror(Enum):
    """Sentinel: the second reading agrees with the first.

    Distinct from ``None``, which means there was no second reading at all -
    the difference the whole ``agreed is None`` branch turns on.
    """

    TOKEN = auto()


MIRROR: Final = _Mirror.TOKEN


def check(
    primary: InvoiceExtraction | None = None,
    secondary: InvoiceExtraction | _Mirror | None = MIRROR,
    *,
    raw_text: str = RAW_51109305,
    vendor_country: str | None = "IN",
    min_sharpness: float | None = None,
) -> ExtractionConfidence:
    """Run the check. Both readings agree unless a different one is passed."""
    base = primary or extraction()
    return compute_extraction_confidence(
        ComputeExtractionConfidenceInput(
            primary=base,
            secondary=base if secondary is MIRROR else secondary,
            raw_text=raw_text,
            vendor_country=vendor_country,
            min_sharpness=min_sharpness,
        )
    ).confidence


# --- the bug this tool was built for ----------------------------------------


def test_the_51109305_date_is_resolved_to_the_indian_reading() -> None:
    """The regression that motivated the whole check.

    ``09/03/2024`` on an Indian vendor's invoice is 9 March, not 3 September.
    The model read it the American way and had no way to know better; the
    vendor's country is the fact that settles it.
    """
    result = check(vendor_country="IN")
    assert result.resolved_invoice_date == date(2024, 3, 9)


def test_the_resolved_date_differs_from_what_the_model_extracted() -> None:
    """If these were equal the test above would pass for the wrong reason."""
    assert extraction().invoice_date == date(2024, 9, 3)
    assert check(vendor_country="IN").resolved_invoice_date == date(2024, 3, 9)


def test_the_same_digits_resolve_the_other_way_for_a_us_vendor() -> None:
    """Same document, same models, different vendor. The rule is locale, not luck."""
    assert check(vendor_country="US").resolved_invoice_date == date(2024, 9, 3)


def test_an_unknown_locale_escalates_rather_than_guessing() -> None:
    """A consistent guess would be worse than none: it would be silently wrong."""
    result = check(vendor_country=None)
    assert result.resolved_invoice_date is None
    assert result.auto_ok is False
    assert AMBIGUOUS_DATE_REASON in result.needs_human


def test_a_resolved_date_is_marked_as_interpreted_not_as_read() -> None:
    """The audit trail must show that a rule touched this field."""
    entry = check(vendor_country="IN").by_field()["invoice_date"]
    assert entry.reason == RESOLVED_DATE_REASON
    assert entry.score < 1.0


def test_resolving_a_date_does_not_by_itself_require_a_human() -> None:
    """The locale rule is trusted; it is only the *absence* of a locale that blocks."""
    assert check(vendor_country="IN").auto_ok is True


# --- what counts as ambiguous ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_ambiguous"),
    [
        ("Date: 09/03/2024", True),
        ("Date: 09.03.2024", True),
        ("Date: 2024-03-09", False),
        ("Date: 09-03-2024", False),
        ("Date: 21/03/2024", False),
        ("Date: 09/21/2024", False),
    ],
    ids=["slash", "dot", "iso", "dash", "day-too-big", "month-too-big"],
)
def test_only_a_slash_or_dot_with_two_plausible_months_is_ambiguous(
    raw: str, expected_ambiguous: bool
) -> None:
    """A dash reads as ISO; a component above 12 settles the order by itself."""
    for candidate in (date(2024, 3, 9), date(2024, 9, 3), date(2024, 3, 21), date(2024, 9, 21)):
        found = find_raw_date(raw, candidate)
        if found is not None:
            assert found.is_ambiguous is expected_ambiguous
            return
    pytest.fail(f"no date found in {raw!r}")


def test_an_unambiguous_date_is_left_exactly_as_extracted() -> None:
    result = check(
        extraction(invoice_date=date(2024, 3, 21)),
        raw_text="Invoice No: 51109305\nDate: 21/03/2024\nTotal 1,67,560.00 INR\n"
        "Sunrise Traders Pvt Ltd\n1,42,000.00\n25,560.00",
    )
    assert result.resolved_invoice_date == date(2024, 3, 21)
    assert result.by_field()["invoice_date"].reason == "agreed_and_grounded"


def test_a_date_absent_from_the_text_is_not_grounded() -> None:
    result = check(extraction(invoice_date=date(2019, 1, 1)))
    entry = result.by_field()["invoice_date"]
    assert entry.grounded is False
    assert result.auto_ok is False


# --- agreement ---------------------------------------------------------------


def test_a_field_both_readings_agree_on_and_the_page_contains_scores_full() -> None:
    entry = check().by_field()["invoice_number"]
    assert (entry.agreed, entry.grounded, entry.score) == (True, True, 1.0)


def test_disagreement_blocks_and_scores_lowest_even_when_grounded() -> None:
    """Grounded but contradicted: one reading is wrong and this cannot say which."""
    result = check(secondary=extraction(total=Decimal("142000.00")))
    entry = result.by_field()["total"]
    assert entry.agreed is False
    assert entry.reason == "readings_disagree"
    assert entry.score < 0.5
    assert result.auto_ok is False
    assert "total" in result.needs_human


def test_amounts_agree_across_separators_and_trailing_zeros() -> None:
    """``167560`` and ``1,67,560.00`` are the same money, written by two models."""
    result = check(secondary=extraction(total=Decimal(167560)))
    assert result.by_field()["total"].agreed is True


def test_a_name_agrees_through_punctuation_and_case() -> None:
    result = check(secondary=extraction(vendor_name="SUNRISE TRADERS PVT. LTD."))
    assert result.by_field()["vendor_name"].agreed is True


def test_a_different_company_does_not_agree() -> None:
    result = check(secondary=extraction(vendor_name="Northwind Logistics GmbH"))
    assert result.by_field()["vendor_name"].agreed is False


def test_name_similarity_is_not_used_for_identifiers() -> None:
    """One transposed digit is a different invoice, not a near-match."""
    result = check(secondary=extraction(invoice_number="51109350"))
    assert result.by_field()["invoice_number"].agreed is False
    assert name_similarity("51109305", "51109350") > 0.8


def test_two_readings_of_an_ambiguous_date_agree_on_the_characters() -> None:
    """They read the same thing. Which way it points is a separate question.

    Reporting this as a disagreement would fire on every ambiguous date and
    drown the cases where the models genuinely read different digits.
    """
    result = check(secondary=extraction(invoice_date=date(2024, 3, 9)))
    assert result.by_field()["invoice_date"].agreed is True


def test_genuinely_different_dates_still_disagree() -> None:
    result = check(secondary=extraction(invoice_date=date(2024, 7, 4)))
    assert result.by_field()["invoice_date"].agreed is False


# --- grounding ---------------------------------------------------------------


def test_an_amount_absent_from_the_document_is_not_grounded() -> None:
    """The hallucination case: confident, agreed, and nowhere on the page."""
    invented = extraction(total=Decimal("999999.00"))
    result = check(invented, invented)
    entry = result.by_field()["total"]
    assert entry.agreed is True
    assert entry.grounded is False
    assert entry.reason == "not_in_text_layer"
    assert result.auto_ok is False


def test_agreement_alone_cannot_make_a_field_pass() -> None:
    """Two models can agree on an invention; grounding is what rules it out."""
    invented = extraction(total=Decimal("999999.00"))
    assert check(invented, invented).by_field()["total"].score < 0.6


def test_indian_digit_grouping_is_found_without_knowing_the_convention() -> None:
    """``1,67,560.00`` groups differently from ``167,560.00``. Both must ground."""
    assert check().by_field()["total"].grounded is True
    western = "Sunrise Traders Pvt Ltd\nInvoice No: 51109305\nDate: 09/03/2024\n"
    western += "Subtotal 142,000.00\nGST 25,560.00\nTotal 167,560.00 INR"
    assert check(raw_text=western).by_field()["total"].grounded is True


def test_an_empty_text_layer_grounds_nothing_and_says_why() -> None:
    """A scan is not a failed extraction; it is an extraction that cannot be checked."""
    result = check(raw_text="")
    assert all(not entry.grounded for entry in result.fields)
    assert all(entry.reason.startswith("no_text_layer") for entry in result.fields)
    assert result.auto_ok is False
    assert set(result.needs_human) == set(LOAD_BEARING_FIELDS)


# --- one reading -------------------------------------------------------------


def test_a_single_reading_leaves_agreement_unknown() -> None:
    """None, not False. Nothing contradicted it - nothing confirmed it either."""
    result = check(secondary=None)
    assert all(entry.agreed is None for entry in result.fields)


def test_a_single_reading_is_never_automatic() -> None:
    result = check(secondary=None)
    assert result.auto_ok is False
    assert result.needs_human == ["single_read"]


def test_a_single_reading_still_scores_above_a_contradicted_one() -> None:
    unconfirmed = check(secondary=None).by_field()["total"].score
    contradicted = check(secondary=extraction(total=Decimal("1.00"))).by_field()["total"].score
    assert contradicted < unconfirmed < 1.0


# --- sharpness ---------------------------------------------------------------


def test_a_blurry_page_lowers_every_score_and_requires_a_human() -> None:
    sharp = check()
    blurry = check(min_sharpness=MIN_SHARPNESS - 1)
    assert blurry.auto_ok is False
    assert "low_sharpness" in blurry.needs_human
    for before, after in zip(sharp.fields, blurry.fields, strict=True):
        assert after.score < before.score
        assert after.reason.endswith("low_sharpness")


def test_a_sharp_page_is_not_penalised() -> None:
    assert check(min_sharpness=MIN_SHARPNESS + 1).auto_ok is True


def test_an_unmeasured_page_is_not_penalised() -> None:
    """No sharpness reading is not the same as a bad one."""
    assert check(min_sharpness=None).auto_ok is True


# --- what auto_ok actually promises ------------------------------------------


@pytest.mark.parametrize("field", LOAD_BEARING_FIELDS)
def test_every_load_bearing_field_can_block_on_its_own(field: str) -> None:
    """Who, which document, when, what currency, how much. Any one of them."""
    broken = {
        "vendor_name": "Northwind Logistics GmbH",
        "invoice_number": "00000000",
        "invoice_date": date(2019, 1, 1),
        "currency": "EUR",
        "total": Decimal("999999.00"),
    }[field]
    result = check(secondary=extraction(**{field: broken}))
    assert result.auto_ok is False
    assert field in result.needs_human


@pytest.mark.parametrize("field", ["subtotal", "tax_total"])
def test_a_supporting_field_is_reported_but_does_not_block(field: str) -> None:
    """A person can correct a subtotal. Paying the wrong vendor is not correctable."""
    result = check(secondary=extraction(**{field: Decimal("1.00")}))
    assert result.by_field()[field].agreed is False
    assert field not in result.needs_human
    assert result.auto_ok is True


def test_auto_ok_means_exactly_that_needs_human_is_empty() -> None:
    assert check().needs_human == []
    assert check().auto_ok is True


def test_every_checked_field_gets_an_entry_exactly_once() -> None:
    fields = [entry.field for entry in check().fields]
    assert fields == [*LOAD_BEARING_FIELDS, "subtotal", "tax_total"]


# --- determinism -------------------------------------------------------------


def test_the_same_inputs_give_the_same_answer() -> None:
    """No clock, no randomness, no model. Replaying an audit must reproduce this."""
    assert check().model_dump() == check().model_dump()


# --- ISO dates, which the first draft of the pattern missed entirely ---------


def test_an_iso_date_in_the_text_layer_is_grounded() -> None:
    """Text layers are full of these, and a day-month pattern matches none of them."""
    raw = RAW_51109305.replace("09/03/2024", "2024-03-09")
    result = check(extraction(invoice_date=date(2024, 3, 9)), raw_text=raw)
    entry = result.by_field()["invoice_date"]
    assert entry.grounded is True
    assert result.resolved_invoice_date == date(2024, 3, 9)


def test_an_iso_date_is_never_treated_as_ambiguous() -> None:
    """The leading year fixes the order, even where both tail parts are small."""
    found = find_raw_date("Date: 2024/03/09", date(2024, 3, 9))
    assert found is not None
    assert found.is_ambiguous is False
    assert found.year_first is True


def test_an_iso_date_needs_no_locale_to_pass() -> None:
    raw = RAW_51109305.replace("09/03/2024", "2024-03-09")
    result = check(extraction(invoice_date=date(2024, 3, 9)), raw_text=raw, vendor_country=None)
    assert result.auto_ok is True
