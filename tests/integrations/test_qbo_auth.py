"""Token refresh, rotation and persistence — with httpx mocked throughout.

Rotation is the part worth testing hardest. Intuit invalidates the previous
refresh token on every successful refresh, so a bug that keeps re-using the
value from `.env` does not fail on the next call - it fails on the one after,
with `invalid_grant` and no obvious cause.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ap_agent.config import Settings
from ap_agent.integrations.qbo.auth import (
    TOKEN_ENDPOINT,
    QboAuthError,
    QboTokenManager,
    TokenSet,
)


def _settings(**overrides: Any) -> Settings:
    payload: dict[str, Any] = {
        "qbo_client_id": SecretStr("client-abc"),
        "qbo_client_secret": SecretStr("secret-xyz"),
        "qbo_realm_id": "1234567890",
        "qbo_refresh_token": SecretStr("seed-refresh-token"),
    }
    payload.update(overrides)
    return Settings(_env_file=None, **payload)  # type: ignore[call-arg]


class RecordingTransport(httpx.BaseTransport):
    """Answers token requests from a script, remembering what was asked."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def _token_response(access: str, refresh: str, expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": access, "refresh_token": refresh, "expires_in": expires_in},
    )


def _manager(
    tmp_path: Path, responses: list[httpx.Response], **setting_overrides: Any
) -> tuple[QboTokenManager, RecordingTransport]:
    transport = RecordingTransport(responses)
    client = httpx.Client(transport=transport)
    manager = QboTokenManager(
        settings=_settings(**setting_overrides),
        token_path=tmp_path / ".qbo_tokens.json",
        http=client,
    )
    return manager, transport


# --- the exchange -----------------------------------------------------------


def test_it_posts_the_documented_refresh_grant(tmp_path: Path) -> None:
    manager, transport = _manager(tmp_path, [_token_response("access-1", "refresh-2")])
    manager.refresh()

    (request,) = transport.requests
    assert str(request.url) == TOKEN_ENDPOINT
    assert request.method == "POST"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.headers["Authorization"].startswith("Basic ")
    assert b"grant_type=refresh_token" in request.content
    assert b"seed-refresh-token" in request.content


def test_the_basic_header_is_the_client_credentials(tmp_path: Path) -> None:
    manager, transport = _manager(tmp_path, [_token_response("a", "b")])
    manager.refresh()
    encoded = transport.requests[0].headers["Authorization"].removeprefix("Basic ")
    assert base64.b64decode(encoded).decode() == "client-abc:secret-xyz"


def test_missing_client_credentials_fail_before_any_request(tmp_path: Path) -> None:
    manager, transport = _manager(tmp_path, [], qbo_client_secret=SecretStr(""))
    with pytest.raises(QboAuthError, match="CLIENT_ID and QBO_CLIENT_SECRET"):
        manager.refresh()
    assert transport.requests == []


def test_no_refresh_token_anywhere_is_a_clear_error(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path, [], qbo_refresh_token=SecretStr(""))
    with pytest.raises(QboAuthError, match="no QuickBooks refresh token"):
        manager.refresh()


# --- rotation and persistence -----------------------------------------------


def test_the_rotated_refresh_token_is_persisted(tmp_path: Path) -> None:
    """The whole point. Intuit kills the old one, so it must be written down."""
    path = tmp_path / ".qbo_tokens.json"
    manager, _ = _manager(tmp_path, [_token_response("access-1", "rotated-token-2")])
    manager.refresh()

    stored: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert stored["refresh_token"] == "rotated-token-2"
    assert stored["realm_id"] == "1234567890"


def test_the_persisted_token_beats_the_setting(tmp_path: Path) -> None:
    """After the first rotation the value in .env is dead; using it locks us out."""
    path = tmp_path / ".qbo_tokens.json"
    path.write_text(json.dumps({"refresh_token": "from-disk"}), encoding="utf-8")

    manager, transport = _manager(tmp_path, [_token_response("a", "b")])
    assert manager.current_refresh_token() == "from-disk"

    manager.refresh()
    assert b"from-disk" in transport.requests[0].content
    assert b"seed-refresh-token" not in transport.requests[0].content


def test_the_setting_bootstraps_when_there_is_no_file(tmp_path: Path) -> None:
    manager, _ = _manager(tmp_path, [_token_response("a", "b")])
    assert manager.current_refresh_token() == "seed-refresh-token"


def test_a_second_refresh_uses_the_token_the_first_one_returned(tmp_path: Path) -> None:
    """The failure this whole design exists to prevent."""
    manager, transport = _manager(
        tmp_path,
        [_token_response("access-1", "rotated-2"), _token_response("access-2", "rotated-3")],
    )
    manager.refresh()
    manager.refresh()

    assert b"seed-refresh-token" in transport.requests[0].content
    assert b"rotated-2" in transport.requests[1].content


def test_an_unreadable_token_file_falls_back_to_the_setting(tmp_path: Path) -> None:
    (tmp_path / ".qbo_tokens.json").write_text("{ not json", encoding="utf-8")
    manager, _ = _manager(tmp_path, [_token_response("a", "b")])
    assert manager.current_refresh_token() == "seed-refresh-token"


def test_the_token_file_is_not_world_readable(tmp_path: Path) -> None:
    path = tmp_path / ".qbo_tokens.json"
    manager, _ = _manager(tmp_path, [_token_response("a", "b")])
    manager.refresh()
    assert path.stat().st_mode & 0o077 == 0


def test_a_response_without_a_new_refresh_token_keeps_the_old_one(tmp_path: Path) -> None:
    """Defensive: losing access because a field was absent would be gratuitous."""
    manager, _ = _manager(
        tmp_path, [httpx.Response(200, json={"access_token": "a", "expires_in": 3600})]
    )
    assert manager.refresh().refresh_token == "seed-refresh-token"


# --- caching ----------------------------------------------------------------


def test_a_fresh_access_token_is_reused(tmp_path: Path) -> None:
    manager, transport = _manager(tmp_path, [_token_response("access-1", "b")])
    assert manager.access_token() == "access-1"
    assert manager.access_token() == "access-1"
    assert len(transport.requests) == 1


def test_a_token_near_expiry_is_refreshed_early(tmp_path: Path) -> None:
    """Valid-when-checked is not valid-when-it-lands on a slow request."""
    manager, transport = _manager(
        tmp_path,
        [_token_response("access-1", "b", expires_in=3600), _token_response("access-2", "c")],
    )
    manager.access_token()
    # Four minutes before expiry: inside the five-minute skew.
    almost = datetime.now(UTC) + timedelta(seconds=3600 - 240)
    assert manager.access_token(now=almost) == "access-2"
    assert len(transport.requests) == 2


def test_token_set_usability_respects_the_skew() -> None:
    now = datetime.now(UTC)
    tokens = TokenSet("a", now + timedelta(minutes=10), "r")
    assert tokens.is_usable(now=now)
    assert not tokens.is_usable(now=now + timedelta(minutes=6))


# --- secrecy ----------------------------------------------------------------


def test_a_token_set_never_prints_its_tokens() -> None:
    """A dataclass repr in a traceback would put both tokens in a log."""
    text = repr(TokenSet("super-secret-access", datetime.now(UTC), "super-secret-refresh"))
    assert "super-secret" not in text
    assert "redacted" in text


def test_a_rejected_refresh_does_not_echo_the_body(tmp_path: Path) -> None:
    manager, _ = _manager(
        tmp_path,
        [httpx.Response(400, json={"error": "invalid_grant", "refresh_token": "leaked-value"})],
    )
    with pytest.raises(QboAuthError) as caught:
        manager.refresh()
    assert "invalid_grant" in str(caught.value)
    assert "leaked-value" not in str(caught.value)
