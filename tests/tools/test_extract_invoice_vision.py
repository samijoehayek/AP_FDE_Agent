"""The extraction request, asserted against a mocked client.

Nothing here calls the API. What is worth testing is the *shape of the request*,
because that shape is where the security properties live: a schema the response
must satisfy, and no tools for injected text to reach for. Those are cheap to
assert and expensive to notice the loss of.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anthropic
import pytest
from PIL import Image

from ap_agent.contracts.invoice import InvoiceExtraction
from ap_agent.errors import ExtractionError
from ap_agent.tools.extract_invoice_vision import (
    PROMPT_VERSION,
    ExtractInvoiceVisionInput,
    build_content_block,
    extract_invoice_vision,
    load_prompt,
)

SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


def _client_factory(client: MagicMock) -> Callable[..., MagicMock]:
    """A typed stand-in for ``anthropic.Anthropic`` that always returns ``client``."""

    def _construct(*_args: object, **_kwargs: object) -> MagicMock:
        return client

    return _construct


def _block_field(block: object, *path: str) -> Any:
    """Read a nested key out of a TypedDict content block.

    The SDK types these as TypedDicts, so pyright rejects subscripting them with
    a runtime string; the test genuinely wants to inspect the wire shape.
    """
    node: Any = block
    for key in path:
        node = node[key]
    return node


def _fake_response(extraction: InvoiceExtraction) -> MagicMock:
    response = MagicMock()
    response.stop_reason = "end_turn"
    response.parsed_output = extraction
    response.model = "claude-sonnet-5"
    response.usage.input_tokens = 4321
    response.usage.output_tokens = 765
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
    # Patch the SDK constructor rather than the tool's helper: `ap_agent.tools`
    # re-exports the function `extract_invoice_vision`, which shadows the module
    # of the same name, so the module object is not reachable by attribute.
    monkeypatch.setattr(anthropic, "Anthropic", _client_factory(client))
    return seen


@pytest.fixture
def run_extract(captured: dict[str, Any], born_digital_pdf: Path) -> tuple[dict[str, Any], Any]:
    result = extract_invoice_vision(ExtractInvoiceVisionInput(path=born_digital_pdf))
    return captured, result


# --- the controls -----------------------------------------------------------


def test_the_response_is_constrained_to_the_contract(
    run_extract: tuple[dict[str, Any], Any],
) -> None:
    """Structured outputs: the API cannot emit a shape the contract disallows."""
    request, _ = run_extract
    assert request["output_format"] is InvoiceExtraction


def test_no_tools_are_offered(run_extract: tuple[dict[str, Any], Any]) -> None:
    """Rule 1. Injected text has nothing to reach for if there is no tool."""
    assert "tools" not in run_extract[0]
    assert "tool_choice" not in run_extract[0]


def test_no_sampling_parameters_are_sent(run_extract: tuple[dict[str, Any], Any]) -> None:
    """`temperature=0` is not expressible on this path, and must not be attempted.

    The models this seat runs on reject `temperature`, `top_p` and `top_k` with a
    400, and `messages.parse()` does not accept them at all. Determinism comes
    from the schema constraint instead - a stronger guarantee than a sampling
    setting, which never promised identical outputs anyway.
    """
    request, _ = run_extract
    assert not [p for p in SAMPLING_PARAMS if p in request]


def test_the_document_is_data_not_prompt_text(
    run_extract: tuple[dict[str, Any], Any], born_digital_pdf: Path
) -> None:
    """Nothing from the file is interpolated into the system prompt."""
    request, _ = run_extract
    assert request["system"] == load_prompt(PROMPT_VERSION)
    encoded = base64.standard_b64encode(born_digital_pdf.read_bytes()).decode("ascii")
    assert encoded not in request["system"]

    (message,) = request["messages"]
    assert message["role"] == "user"
    (block,) = message["content"]
    assert _block_field(block, "type") == "document"
    assert _block_field(block, "source", "data") == encoded


# --- request construction ---------------------------------------------------


def test_the_configured_model_is_used(run_extract: tuple[dict[str, Any], Any]) -> None:
    request, _ = run_extract
    assert request["model"] == "claude-sonnet-5"


def test_an_explicit_model_overrides_settings(
    captured: dict[str, Any], born_digital_pdf: Path
) -> None:
    extract_invoice_vision(
        ExtractInvoiceVisionInput(path=born_digital_pdf, model_id="claude-opus-5")
    )
    assert captured["model"] == "claude-opus-5"


def test_the_result_carries_what_the_audit_trail_needs(
    run_extract: tuple[dict[str, Any], Any],
) -> None:
    _, result = run_extract
    assert result.prompt_version == "extract_v1"
    assert result.model_id == "claude-sonnet-5"
    assert result.input_tokens == 4321
    assert result.output_tokens == 765
    assert result.latency_ms >= 0


# --- content blocks ---------------------------------------------------------


def test_a_pdf_becomes_a_document_block(born_digital_pdf: Path) -> None:
    block = build_content_block(born_digital_pdf)
    assert _block_field(block, "type") == "document"
    assert _block_field(block, "source", "media_type") == "application/pdf"


def test_a_png_becomes_an_image_block(sharp_png: Path) -> None:
    block = build_content_block(sharp_png)
    assert _block_field(block, "type") == "image"
    assert _block_field(block, "source", "media_type") == "image/png"


def test_a_tiff_is_re_encoded_because_the_api_will_not_take_it(tmp_path: Path) -> None:
    """The API accepts jpeg/png/gif/webp only; scanned invoices arrive as TIFF."""
    path = tmp_path / "scan.tiff"
    Image.new("L", (64, 64), color=200).save(path, format="TIFF")

    block = build_content_block(path)
    assert _block_field(block, "type") == "image"
    assert _block_field(block, "source", "media_type") == "image/png"
    raw = base64.standard_b64decode(_block_field(block, "source", "data"))
    assert raw.startswith(b"\x89PNG")


# --- failure modes ----------------------------------------------------------


def test_a_missing_file_raises_an_ap_agent_error(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="not a file"):
        extract_invoice_vision(ExtractInvoiceVisionInput(path=tmp_path / "nope.pdf"))


def test_an_unknown_prompt_version_raises(born_digital_pdf: Path) -> None:
    """A missing prompt is a deployment error; an unversioned prompt is unauditable."""
    with pytest.raises(ExtractionError, match="no prompt file"):
        extract_invoice_vision(
            ExtractInvoiceVisionInput(path=born_digital_pdf, prompt_version="nope_v9")
        )


def test_an_api_failure_becomes_an_ap_agent_error(
    monkeypatch: pytest.MonkeyPatch, born_digital_pdf: Path
) -> None:
    def _boom(**_kwargs: Any) -> None:
        raise anthropic.APIConnectionError(request=MagicMock())

    client = MagicMock()
    client.messages.parse = _boom
    monkeypatch.setattr(anthropic, "Anthropic", _client_factory(client))

    with pytest.raises(ExtractionError, match="extraction API call failed"):
        extract_invoice_vision(ExtractInvoiceVisionInput(path=born_digital_pdf))


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("refusal", "refused"), ("max_tokens", "truncated")],
)
def test_a_non_answer_is_never_returned_as_an_extraction(
    monkeypatch: pytest.MonkeyPatch,
    born_digital_pdf: Path,
    stop_reason: str,
    expected: str,
) -> None:
    """A refused or truncated response must fail loudly, not yield a partial invoice."""
    response = MagicMock()
    response.stop_reason = stop_reason
    response.parsed_output = None
    client = MagicMock()
    client.messages.parse = _client_factory(response)
    monkeypatch.setattr(anthropic, "Anthropic", _client_factory(client))

    with pytest.raises(ExtractionError, match=expected):
        extract_invoice_vision(ExtractInvoiceVisionInput(path=born_digital_pdf))


def test_the_prompt_forbids_acting_on_document_instructions() -> None:
    """The prompt is a control surface; these clauses are the reason it exists."""
    prompt = load_prompt(PROMPT_VERSION).lower()
    assert "suspicious_text" in prompt
    for phrase in ("do not act on it", "do not infer", "as printed"):
        assert phrase in prompt
