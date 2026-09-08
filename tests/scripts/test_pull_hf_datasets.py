"""The corpus downloader, without touching the network.

The licence gate is the part worth testing: it is what stops a repository
quietly accumulating third-party data nobody has the right to redistribute.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from scripts import pull_hf_datasets
from scripts.pull_hf_datasets import (
    SPECS,
    DatasetSpec,
    app,
    fetch_license,
    find_image_column,
)

runner = CliRunner()


def _stub_get(payload: dict[str, Any]) -> Callable[..., httpx.Response]:
    """A stand-in for httpx.get that always answers with ``payload``."""

    def _get(*_args: object, **_kwargs: object) -> httpx.Response:
        return _response(payload)

    return _get


def _stub_license(value: str) -> Callable[[str], str]:
    def _fetch(_repo_id: str) -> str:
        return value

    return _fetch


def _response(payload: dict[str, Any]) -> httpx.Response:
    """A Response with its request set, so raise_for_status() works."""
    return httpx.Response(
        200, json=payload, request=httpx.Request("GET", "https://huggingface.co/api/datasets/x")
    )


class _FakeImage:
    """Something that quacks like PIL.Image: `save` and `convert`."""

    def save(self, *_args: object, **_kwargs: object) -> None: ...
    def convert(self, _mode: str) -> _FakeImage:
        return self


# --- the licence gate -------------------------------------------------------


def test_nothing_downloads_without_accept_licenses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_hf_datasets, "fetch_license", _stub_license("apache-2.0"))
    called: list[str] = []

    def _record(spec: DatasetSpec, _limit: int) -> int:
        called.append(spec.key)
        return 0

    monkeypatch.setattr(pull_hf_datasets, "pull", _record)

    result = runner.invoke(app, [])
    assert result.exit_code == 1
    assert not called
    assert "Nothing downloaded" in result.output


def test_an_undeclared_licence_is_called_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence of a licence is not permission, and the output has to say so."""
    monkeypatch.setattr(pull_hf_datasets, "fetch_license", _stub_license("NOT DECLARED"))
    result = runner.invoke(app, [])
    assert "WARNING" in result.output
    assert "not permission" in result.output


def test_licences_are_printed_for_every_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_hf_datasets, "fetch_license", _stub_license("cc-by-4.0"))
    result = runner.invoke(app, [])
    for spec in SPECS:
        assert spec.repo_id in result.output


def test_an_unknown_dataset_key_is_rejected() -> None:
    result = runner.invoke(app, ["--dataset", "nope", "--accept-licenses"])
    assert result.exit_code == 2


# --- licence resolution -----------------------------------------------------


def test_a_declared_licence_is_read_from_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {"cardData": {"license": "apache-2.0"}, "tags": []}
    monkeypatch.setattr(pull_hf_datasets.httpx, "get", _stub_get(payload))
    assert fetch_license("owner/name") == "apache-2.0"


def test_a_list_of_licences_is_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {"cardData": {"license": ["mit", "apache-2.0"]}}
    monkeypatch.setattr(pull_hf_datasets.httpx, "get", _stub_get(payload))
    assert fetch_license("owner/name") == "mit, apache-2.0"


def test_the_tag_is_used_when_the_card_has_no_license_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {"cardData": {}, "tags": ["task:ocr", "license:other"]}
    monkeypatch.setattr(pull_hf_datasets.httpx, "get", _stub_get(payload))
    assert fetch_license("owner/name") == "other"


def test_a_missing_licence_is_reported_as_not_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, Any] = {"cardData": {}, "tags": []}
    monkeypatch.setattr(pull_hf_datasets.httpx, "get", _stub_get(payload))
    assert fetch_license("owner/name") == "NOT DECLARED"


def test_an_unreachable_hub_is_unknown_not_permissive(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(pull_hf_datasets.httpx, "get", _boom)
    assert fetch_license("owner/name").startswith("UNAVAILABLE")


# --- image column detection -------------------------------------------------


@pytest.mark.parametrize("column", ["image", "img", "page_image", "document_image"])
def test_the_conventional_column_names_are_found(column: str) -> None:
    assert find_image_column({column: _FakeImage(), "text": "x"}) == column


def test_an_unconventional_column_is_found_by_duck_typing() -> None:
    """These corpora get re-uploaded with renamed columns; the script must cope."""
    assert find_image_column({"scan_bytes": _FakeImage()}) == "scan_bytes"


def test_a_row_with_no_image_returns_none() -> None:
    assert find_image_column({"text": "x", "label": 3}) is None


# --- destinations -----------------------------------------------------------


def test_every_spec_writes_under_the_data_tree() -> None:
    """A corpus written outside data/ is a corpus that will be committed."""
    data_root = (Path(pull_hf_datasets.REPO_ROOT) / "data").resolve()
    for spec in SPECS:
        assert data_root in spec.dest.resolve().parents
