"""Content-addressed payloads: what a reference is computed over is what is stored."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from ap_agent.contracts.screening import OutputFlag
from ap_agent.payloads import (
    JSON,
    TEXT,
    InMemoryPayloadStore,
    content_ref,
    parse_ref,
    serialise,
    store_ref,
)
from ap_agent.tools.classify_exception import ClassifyExceptionOutput


def test_the_stored_bytes_are_the_hashed_bytes() -> None:
    store = InMemoryPayloadStore()
    value = OutputFlag(check="iban", field="payment_terms")

    ref = store_ref(store, value)

    stored = store.get(parse_ref(ref))
    assert stored is not None
    assert "sha256:" + hashlib.sha256(stored.content).hexdigest() == ref
    assert ref == content_ref(value), "storing does not change the reference"
    assert stored.media_type == JSON


def test_a_string_is_stored_as_text() -> None:
    store = InMemoryPayloadStore()
    stored = store.get(parse_ref(store_ref(store, "po_not_found:AP-1")))
    assert stored is not None
    assert (stored.content, stored.media_type) == (b"po_not_found:AP-1", TEXT)


def test_what_a_call_cost_is_not_part_of_what_it_returned() -> None:
    """Latency and tokens measure the call; the same answer must hash the same."""

    def output(latency: int) -> ClassifyExceptionOutput:
        return ClassifyExceptionOutput(
            classification=None,
            rejected="x",
            model_id="m",
            prompt_version="classify_v1",
            input_tokens=latency,
            output_tokens=latency,
            latency_ms=latency,
        )

    assert content_ref(output(1)) == content_ref(output(9999))
    assert b"latency_ms" not in serialise(output(1))[0]


def test_writing_the_same_bytes_twice_is_one_payload() -> None:
    store = InMemoryPayloadStore()
    assert store_ref(store, "same") == store_ref(store, "same")
    assert len(store.payloads) == 1


@pytest.mark.parametrize("ref", ["sha256:" + "A" * 64, "a" * 64, "  sha256:" + "0" * 64 + " "])
def test_a_reference_parses_bare_or_prefixed(ref: str) -> None:
    assert len(parse_ref(ref)) == 64
    assert parse_ref(ref) == parse_ref(ref).lower()


@pytest.mark.parametrize("ref", ["", "sha256:", "sha256:abc", "g" * 64, "sha1:" + "0" * 40])
def test_anything_else_is_not_a_reference(ref: str) -> None:
    with pytest.raises(ValueError, match="not a sha256 reference"):
        parse_ref(ref)


def test_a_missing_payload_is_none() -> None:
    assert InMemoryPayloadStore().get("0" * 64) is None


def test_decimals_serialise_as_strings() -> None:
    """A float in a stored payload would not be the number that was decided on."""
    from ap_agent.contracts.exceptions import HeaderNumbers  # noqa: PLC0415

    content, _ = serialise(
        HeaderNumbers(
            currency="USD",
            subtotal=Decimal("10.10"),
            tax_total=Decimal("0.00"),
            total=Decimal("10.10"),
            invoice_date=__import__("datetime").date(2026, 1, 1),
            line_count=1,
        )
    )
    assert b'"10.10"' in content
