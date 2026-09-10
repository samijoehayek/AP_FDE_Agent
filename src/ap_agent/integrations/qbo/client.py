"""A thin REST client for the QuickBooks Online accounting API.

Deliberately thin. It authenticates, sends, retries the failures worth retrying,
and turns everything else into one typed error. It knows nothing about invoices,
purchase orders or matching - that separation is what keeps "what QuickBooks
calls a purchase order" from leaking into "what this system does about one".

Verified against Intuit's current documentation:

* Sandbox ``https://sandbox-quickbooks.api.intuit.com/v3/company/{realmId}/``
* Production ``https://quickbooks.api.intuit.com/v3/company/{realmId}/``
* ``minorversion`` below 75 is ignored and served as 75, so 75 is the floor.

Nothing here logs a token. The access token exists only inside the Authorization
header of a request that is never logged whole, and the token manager redacts
its own repr - because an integration's error path is exactly the log line most
likely to end up pasted into a ticket.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from ap_agent.config import get_settings
from ap_agent.errors import APAgentError
from ap_agent.integrations.qbo.auth import QboTokenManager
from ap_agent.logging import get_logger

if TYPE_CHECKING:
    from ap_agent.config import Settings

log = get_logger(__name__)

SANDBOX_BASE_URL: Final = "https://sandbox-quickbooks.api.intuit.com/v3/company"
PRODUCTION_BASE_URL: Final = "https://quickbooks.api.intuit.com/v3/company"

MINOR_VERSION: Final = "75"
"""Anything lower is ignored by Intuit and served as 75, so pin it explicitly."""

MAX_ATTEMPTS: Final = 4
BASE_BACKOFF: Final = 1.0
MAX_BACKOFF: Final = 20.0

RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
"""Throttling and server faults. Everything else is a request problem: retrying
a 400 just sends the same bad request again, and retrying a 401 burns the rate
limit while the credential stays wrong.
"""


class QboError(APAgentError):
    """A QuickBooks request failed.

    Carries the status and Intuit's fault detail, never the token.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class QboClient:
    """Authenticated access to one QuickBooks company."""

    def __init__(
        self,
        settings: Settings | None = None,
        tokens: QboTokenManager | None = None,
        http: httpx.Client | None = None,
        sleep: object = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._tokens = tokens or QboTokenManager(self._settings)
        self._http = http
        self._sleep = sleep if callable(sleep) else time.sleep

        if not self._settings.qbo_realm_id:
            msg = "QBO_REALM_ID is not set: there is no company to talk to"
            raise QboError(msg)

    @property
    def base_url(self) -> str:
        """Company-scoped base URL for the configured environment."""
        root = (
            PRODUCTION_BASE_URL
            if self._settings.qbo_environment == "production"
            else SANDBOX_BASE_URL
        )
        return f"{root}/{self._settings.qbo_realm_id}"

    # --- verbs ---------------------------------------------------------

    def get(self, entity: str, entity_id: str) -> dict[str, Any]:
        """Read one record, e.g. ``get("purchaseorder", "123")``."""
        return self._request("GET", f"{entity.lower()}/{entity_id}")

    def query(self, statement: str) -> dict[str, Any]:
        """Run a QuickBooks query, e.g. ``SELECT * FROM Vendor WHERE ...``.

        The statement is sent as a query parameter, so httpx does the escaping.
        Callers must still not interpolate untrusted text into it - the same
        reasoning as any SQL-shaped string, and nothing read off an invoice ever
        reaches here.
        """
        return self._request("GET", "query", params={"query": statement})

    def create(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one record. The only mutating verb this client exposes.

        There is no ``update`` and no ``delete``. Neither is needed by anything
        in this system, and a client that cannot express them is a client no
        future bug can use to express them.
        """
        return self._request("POST", entity.lower(), json=payload)

    # --- transport -----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path}"
        query = {"minorversion": MINOR_VERSION, **(params or {})}

        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {
                "Authorization": f"Bearer {self._tokens.access_token()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            try:
                response = self._send(method, url, headers=headers, params=query, json=json)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == MAX_ATTEMPTS:
                    break
                self._back_off(attempt, None)
                continue

            if response.is_success:
                parsed: dict[str, Any] = response.json()
                return parsed

            if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                log.warning(
                    "qbo_retrying",
                    status=response.status_code,
                    attempt=attempt,
                    path=path,
                )
                self._back_off(attempt, response.headers.get("Retry-After"))
                continue

            msg = f"QuickBooks {method} {path} failed: {_fault(response)}"
            raise QboError(msg, status_code=response.status_code)

        msg = f"QuickBooks {method} {path} unreachable after {MAX_ATTEMPTS} attempts: {last_error}"
        raise QboError(msg)

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        if self._http is not None:
            return self._http.request(method, url, headers=headers, params=params, json=json)
        with httpx.Client(timeout=60.0) as client:
            return client.request(method, url, headers=headers, params=params, json=json)

    def _back_off(self, attempt: int, retry_after: str | None) -> None:
        """Wait before retrying, honouring Retry-After when Intuit sends one.

        Jittered, because a fleet that retries on the same schedule re-creates
        the burst that caused the throttling.
        """
        if retry_after and retry_after.isdigit():
            delay = min(float(retry_after), MAX_BACKOFF)
        else:
            delay = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
        self._sleep(delay + random.uniform(0, 0.5))  # noqa: S311 - jitter, not crypto


def _fault(response: httpx.Response) -> str:
    """Summarise Intuit's Fault object without echoing the whole body."""
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        return f"HTTP {response.status_code}, unparseable body"

    fault: dict[str, Any] = body.get("Fault") or body.get("fault") or {}
    errors: list[Any] = fault.get("Error") or fault.get("error") or []
    if errors:
        head: Any = errors[0]
        first: dict[str, Any] = cast("dict[str, Any]", head) if isinstance(head, dict) else {}
        code = first.get("code", "?")
        message = first.get("Message") or first.get("message") or ""
        detail = first.get("Detail") or first.get("detail") or ""
        return f"HTTP {response.status_code}, code {code}: {message} {detail}".strip()
    return f"HTTP {response.status_code}"
