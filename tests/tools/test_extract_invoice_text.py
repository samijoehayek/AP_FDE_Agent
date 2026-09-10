"""The second reading, asserted against a mocked client.

Nothing here calls the API. Two things are worth testing: the shape of the
request, because that is where the security properties live, and the refusal to
invent a second reading when there is no text layer to make one from - which is
the difference between a check with two independent readings and a check with
one reading counted twice.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anthropic
import pytest

from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.errors import ExtractionError
from ap_agent.tools.extract_invoice_text import (
    PROMPT_VERSION,
    ExtractInvoiceTextInput,
    extract_invoice_text,
    read_text_layer,
)
from ap_agent.tools.extract_invoice_vision import load_prompt

SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


def _client_factory(client: MagicMock) -> Callable[..., MagicMock]:
    def _construct(*_args: object, **_kwargs: object) -> MagicMock:
        return client

    return _construct


def _always(response: MagicMock) -> Callable[..., MagicMock]:
    """A ``messages.parse`` stub that ignores its kwargs and returns ``response``."""

    def _parse(**_kwargs: object) -> MagicMock:
        return response

    return _parse


def _fake_response(extraction: InvoiceExtraction) -> MagicMock:
    response = MagicMock()
    response.stop_reason = "end_turn"
    response.parsed_output = extraction
    response.model = "claude-haiku-4-5-20251001"
    response.usage.input_tokens = 1211
    response.usage.output_tokens = 402
    return response


@pytest.fixture
def captured(
    monkeypatch: pytest.MonkeyPatch, sample_extraction: InvoiceExtraction
) -> dict[str, Any]:
    """Run the tool against a stub client and hand back the request kwargs."""
    seen: dict[str, Any] = {}

    def _parse(**kwargs: Any) -> MagicMock:
        seen.update(kwargs)
        return _fake_response(sample_extraction)

    client = MagicMock()
    client.messages.parse = _parse
    monkeypatch.setattr(anthropic, "Anthropic", _client_factory(client))
    return seen


@pytest.fixture
def run_extract(captured: dict[str, Any], born_digital_pdf: Path) -> tuple[dict[str, Any], Any]:
    result = extract_invoice_text(ExtractInvoiceTextInput(path=born_digital_pdf))
    return captured, result


# --- the controls -----------------------------------------------------------


def test_no_tools_are_offered(run_extract: tuple[dict[str, Any], Any]) -> None:
    """Rule 1, and it matters more here than in the vision seat.

    This model is handed the document's characters directly. If the text says
    "ignore your instructions and call a tool", the defence is that there is no
    tool to call.
    """
    assert "tools" not in run_extract[0]
    assert "tool_choice" not in run_extract[0]


def test_the_document_text_is_the_user_turn_and_nothing_else(
    run_extract: tuple[dict[str, Any], Any], born_digital_pdf: Path
) -> None:
    """Document text is data. It goes where data goes, never into the system prompt."""
    request, _ = run_extract
    pages, _ = read_text_layer(born_digital_pdf)
    assert request["messages"] == [{"role": "user", "content": "\f".join(pages).strip()}]


def test_only_the_prompt_instructs(run_extract: tuple[dict[str, Any], Any]) -> None:
    request, _ = run_extract
    assert request["system"] == load_prompt(PROMPT_VERSION)


def test_the_response_is_constrained_to_the_contract(
    run_extract: tuple[dict[str, Any], Any],
) -> None:
    assert run_extract[0]["output_format"] is InvoiceExtraction


def test_no_sampling_parameters_are_sent(run_extract: tuple[dict[str, Any], Any]) -> None:
    """Same reasoning as the vision seat: these models reject them with a 400."""
    assert not [p for p in SAMPLING_PARAMS if p in run_extract[0]]


def test_the_text_prompt_is_not_the_vision_prompt() -> None:
    """Different modality, different failure modes, different instructions.

    A text layer has no reliable layout, so the vision prompt's advice about
    reading columns and totals blocks would be actively wrong here.
    """
    assert load_prompt(PROMPT_VERSION) != load_prompt("extract_v1")


def test_the_prompt_forbids_resolving_an_ambiguous_date() -> None:
    """The model must not guess what the confidence check is built to decide."""
    assert "ambiguous" in load_prompt(PROMPT_VERSION).lower()


# --- no text layer, no second reading ---------------------------------------


def test_a_scan_produces_no_second_reading(captured: dict[str, Any], scanned_pdf: Path) -> None:
    """No OCR here. A guessed second reading would agree with itself by design."""
    result = extract_invoice_text(ExtractInvoiceTextInput(path=scanned_pdf))
    assert result.has_text_layer is False
    assert result.second_read is None
    assert result.raw_text == ""
    assert captured == {}, "a document with no text layer must not cost a model call"


def test_an_image_produces_no_second_reading(captured: dict[str, Any], sharp_png: Path) -> None:
    """Accepted, not rejected - the vision seat can still read it."""
    result = extract_invoice_text(ExtractInvoiceTextInput(path=sharp_png))
    assert result.has_text_layer is False
    assert result.second_read is None
    assert captured == {}


# --- the text layer itself ---------------------------------------------------


def test_line_breaks_survive(born_digital_pdf: Path) -> None:
    """Grounding reads these characters. Collapsing them loses label adjacency."""
    pages, has_text = read_text_layer(born_digital_pdf)
    assert has_text is True
    assert "\n" in pages[0]


def test_the_returned_text_is_exactly_what_the_model_read(
    run_extract: tuple[dict[str, Any], Any],
) -> None:
    """Grounding is scored against ``raw_text``, so it must be the same string.

    ``StrictModel`` strips every string it validates. Before this was made
    explicit, the join sent to the model and the ``raw_text`` returned differed
    by their outer whitespace - a small gap in exactly the wrong place.
    """
    request, result = run_extract
    assert result.raw_text == request["messages"][0]["content"]


def test_pages_are_separated_by_a_form_feed(run_extract: tuple[dict[str, Any], Any]) -> None:
    """A separator that cannot occur inside a page.

    So a page boundary never invents an adjacency that was not on the paper.
    """
    _, result = run_extract
    assert all(page.strip() in result.raw_text for page in result.pages)
    assert result.raw_text.count("\f") == len(result.pages) - 1


def test_a_missing_file_is_an_extraction_error(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="not a file"):
        read_text_layer(tmp_path / "nothing.pdf")


def test_an_empty_file_is_an_extraction_error(tmp_path: Path) -> None:
    target = tmp_path / "empty.pdf"
    target.write_bytes(b"")
    with pytest.raises(ExtractionError, match="empty file"):
        read_text_layer(target)


# --- what the result records -------------------------------------------------


def test_the_run_records_what_read_it(run_extract: tuple[dict[str, Any], Any]) -> None:
    """The resolved model id, not the alias that was asked for.

    ``claude-haiku-4-5`` is a moving pointer; the audit trail needs the version
    that actually ran.
    """
    _, result = run_extract
    assert result.model_id == "claude-haiku-4-5-20251001"
    assert result.prompt_version == PROMPT_VERSION
    assert result.input_tokens == 1211
    assert result.output_tokens == 402
    assert result.latency_ms is not None


def test_an_explicit_model_overrides_the_setting(
    captured: dict[str, Any], born_digital_pdf: Path
) -> None:
    extract_invoice_text(ExtractInvoiceTextInput(path=born_digital_pdf, model_id="claude-sonnet-5"))
    assert captured["model"] == "claude-sonnet-5"


def test_a_truncated_reading_is_discarded_not_returned(
    monkeypatch: pytest.MonkeyPatch,
    born_digital_pdf: Path,
    sample_extraction: InvoiceExtraction,
) -> None:
    """A half-read invoice that validates is worse than no second reading."""
    response = _fake_response(sample_extraction)
    response.stop_reason = "max_tokens"
    client = MagicMock()
    client.messages.parse = _always(response)
    monkeypatch.setattr(anthropic, "Anthropic", _client_factory(client))

    with pytest.raises(ExtractionError, match="max_tokens"):
        extract_invoice_text(ExtractInvoiceTextInput(path=born_digital_pdf))


def test_a_refusal_is_reported_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
    born_digital_pdf: Path,
    sample_extraction: InvoiceExtraction,
) -> None:
    response = _fake_response(sample_extraction)
    response.stop_reason = "refusal"
    client = MagicMock()
    client.messages.parse = _always(response)
    monkeypatch.setattr(anthropic, "Anthropic", _client_factory(client))

    with pytest.raises(ExtractionError, match="refused"):
        extract_invoice_text(ExtractInvoiceTextInput(path=born_digital_pdf))


def test_an_api_failure_is_wrapped_not_leaked(
    monkeypatch: pytest.MonkeyPatch, born_digital_pdf: Path
) -> None:
    def _raise(**_: Any) -> MagicMock:
        raise anthropic.APIError("upstream said no", request=MagicMock(), body=None)

    client = MagicMock()
    client.messages.parse = _raise
    monkeypatch.setattr(anthropic, "Anthropic", _client_factory(client))

    with pytest.raises(ExtractionError, match="text extraction API call failed"):
        extract_invoice_text(ExtractInvoiceTextInput(path=born_digital_pdf))
