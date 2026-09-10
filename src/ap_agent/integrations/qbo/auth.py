"""OAuth 2.0 token management for QuickBooks Online.

Intuit's refresh tokens **rotate**. Every successful refresh returns a new
refresh token and forces the previous one to expire, which makes the token a
piece of mutable state rather than a credential you can paste into a config file
and forget. Getting that wrong is the classic QuickBooks integration failure:
the app keeps refreshing with the token from ``.env``, Intuit keeps invalidating
it, and one day everything returns ``invalid_grant`` with no obvious cause.

So this module treats the file as the source of truth and the setting as a seed.
On the first run there is no file and the value from ``Settings`` bootstraps the
exchange; from then on the rotated token is written to disk and the setting is
ignored. That ordering is the whole point - the reverse would re-use a dead
token after the first rotation.

Verified against Intuit's current documentation before writing:

* Token endpoint ``https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer``,
  POST, HTTP Basic with the client id and secret, form-encoded body.
* Access tokens last about an hour; refresh tokens about 100 days with a rolling
  expiry, and rotate on every refresh.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx

from ap_agent.config import REPO_ROOT, get_settings
from ap_agent.errors import APAgentError
from ap_agent.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from ap_agent.config import Settings

log = get_logger(__name__)

TOKEN_ENDPOINT: Final = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"  # noqa: S105
"""Intuit's OAuth 2.0 token endpoint. Same host for sandbox and production."""

DEFAULT_TOKEN_PATH: Final = REPO_ROOT / "data" / ".qbo_tokens.json"
"""Under ``data/``, so it is git-ignored along with everything else there."""

EXPIRY_SKEW: Final = timedelta(minutes=5)
"""Refresh this long before the token actually expires.

A token that is valid when checked can still expire in flight on a slow request.
Five minutes is far more than any request here takes and costs one extra refresh
an hour.
"""

FALLBACK_EXPIRES_IN: Final = 3600
"""Assume an hour if the response omits ``expires_in``. Documented value."""


class QboAuthError(APAgentError):
    """Acquiring or refreshing a QuickBooks access token failed.

    Carries no token material. An auth error is exactly the log line most likely
    to be pasted into a ticket.
    """


@dataclass(frozen=True)
class TokenSet:
    """An access token, when it expires, and the refresh token that produced it."""

    access_token: str
    expires_at: datetime
    refresh_token: str

    def is_usable(self, *, now: datetime | None = None) -> bool:
        """True while the access token has more than :data:`EXPIRY_SKEW` left."""
        return (now or datetime.now(UTC)) < self.expires_at - EXPIRY_SKEW

    def __repr__(self) -> str:
        """Redacted. A dataclass repr in a traceback would print both tokens."""
        return f"TokenSet(expires_at={self.expires_at.isoformat()}, tokens=<redacted>)"


class QboTokenManager:
    """Hands out a valid access token, refreshing and persisting as needed."""

    def __init__(
        self,
        settings: Settings | None = None,
        token_path: Path | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._path = token_path or DEFAULT_TOKEN_PATH
        self._http = http
        self._cached: TokenSet | None = None

    # --- public --------------------------------------------------------

    def access_token(self, *, now: datetime | None = None) -> str:
        """Return a usable access token, refreshing if the cached one is stale."""
        if self._cached is not None and self._cached.is_usable(now=now):
            return self._cached.access_token
        return self.refresh().access_token

    def refresh(self) -> TokenSet:
        """Exchange the current refresh token for a new access token.

        Persists the rotated refresh token before returning. Persisting first
        matters: if the process died between the exchange and the write, the
        token on disk would already have been invalidated by Intuit and the
        integration would be locked out until someone re-authorised by hand.
        """
        refresh_token = self.current_refresh_token()
        if not refresh_token:
            msg = (
                "no QuickBooks refresh token: set QBO_REFRESH_TOKEN once to bootstrap, "
                f"or restore {self._path.name}"
            )
            raise QboAuthError(msg)

        payload = self._exchange(refresh_token)
        tokens = self._to_token_set(payload, previous=refresh_token)

        self._persist(tokens)
        self._cached = tokens
        log.info(
            "qbo_token_refreshed",
            expires_at=tokens.expires_at.isoformat(),
            rotated=tokens.refresh_token != refresh_token,
        )
        return tokens

    def current_refresh_token(self) -> str:
        """Return the refresh token to use next.

        The file wins over the setting. After the first rotation the value in
        ``.env`` is dead, and preferring it would lock the integration out.
        """
        stored = self._read_file()
        if stored:
            return stored
        return self._settings.qbo_refresh_token.get_secret_value()

    # --- internals -----------------------------------------------------

    def _basic_auth(self) -> str:
        client_id = self._settings.qbo_client_id.get_secret_value()
        secret = self._settings.qbo_client_secret.get_secret_value()
        if not client_id or not secret:
            msg = "QBO_CLIENT_ID and QBO_CLIENT_SECRET must both be set"
            raise QboAuthError(msg)
        raw = f"{client_id}:{secret}".encode()
        return base64.b64encode(raw).decode("ascii")

    def _exchange(self, refresh_token: str) -> dict[str, Any]:
        """POST the refresh grant and return the parsed body."""
        headers = {
            "Authorization": f"Basic {self._basic_auth()}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        try:
            if self._http is not None:
                response = self._http.post(TOKEN_ENDPOINT, headers=headers, data=form)
            else:
                with httpx.Client(timeout=30.0) as client:
                    response = client.post(TOKEN_ENDPOINT, headers=headers, data=form)
        except httpx.HTTPError as exc:
            msg = f"could not reach the Intuit token endpoint: {exc}"
            raise QboAuthError(msg) from exc

        if not response.is_success:
            # The body of a failed token exchange can echo request material, so
            # only the documented error code is surfaced.
            code = _error_code(response)
            msg = f"token refresh rejected by Intuit (HTTP {response.status_code}, {code})"
            raise QboAuthError(msg)

        body: dict[str, Any] = response.json()
        return body

    def _to_token_set(self, payload: dict[str, Any], *, previous: str) -> TokenSet:
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            msg = "token response contained no access_token"
            raise QboAuthError(msg)

        expires_in = payload.get("expires_in", FALLBACK_EXPIRES_IN)
        seconds = int(expires_in) if isinstance(expires_in, (int, float, str)) else 0
        rotated = payload.get("refresh_token")

        return TokenSet(
            access_token=access,
            expires_at=datetime.now(UTC) + timedelta(seconds=seconds or FALLBACK_EXPIRES_IN),
            # Intuit returns a new refresh token on every refresh, but fall back
            # to the one just used rather than losing access if it ever does not.
            refresh_token=rotated if isinstance(rotated, str) and rotated else previous,
        )

    def _read_file(self) -> str:
        if not self._path.is_file():
            return ""
        try:
            stored: dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("qbo_token_file_unreadable", path=str(self._path))
            return ""
        token = stored.get("refresh_token")
        return token if isinstance(token, str) else ""

    def _persist(self, tokens: TokenSet) -> None:
        """Write the rotated refresh token, atomically and privately.

        Written to a temporary file in the same directory and renamed, because
        ``rename`` is atomic on POSIX: a crash mid-write leaves the previous file
        intact rather than a truncated one, and a truncated token file means a
        manual re-authorisation.

        Only the refresh token is stored. The access token expires within the
        hour and writing it would put a second credential on disk for no gain.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "refresh_token": tokens.refresh_token,
                "rotated_at": datetime.now(UTC).isoformat(),
                "realm_id": self._settings.qbo_realm_id,
            },
            indent=2,
        )

        handle, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".qbo_", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(body + "\n")
            os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR)  # noqa: PTH101 - fd-safe path
            os.replace(temp_name, self._path)  # noqa: PTH105 - atomic rename
        except BaseException:
            with suppress(OSError):
                os.unlink(temp_name)  # noqa: PTH108 - best-effort cleanup
            raise


def _error_code(response: httpx.Response) -> str:
    """Extract Intuit's documented error code without echoing the body."""
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        return "unparseable body"
    code = body.get("error")
    return str(code) if code else "no error code"
