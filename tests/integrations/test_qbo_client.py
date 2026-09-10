"""The QuickBooks REST client: URLs, retries, and what it refuses to leak."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ap_agent.config import Settings
from ap_agent.integrations.qbo.client import (
    MAX_ATTEMPTS,
    MINOR_VERSION,
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    QboClient,
    QboError,
)


class StubTokens:
    """Stands in for the token manager. Never touches the network."""

    def __init__(self, token: str = "access-token-123") -> None:
        self.token = token
        self.calls = 0

    def access_token(self) -> str:
        self.calls += 1
        return self.token


class ScriptedTransport(httpx.BaseTransport):
    """Replays a fixed list of responses and records the requests."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else httpx.Response(200, json={})


def _client(
    responses: list[httpx.Response], environment: str = "sandbox"
) -> tuple[QboClient, ScriptedTransport, list[float]]:
    transport = ScriptedTransport(responses)
    slept: list[float] = []
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        qbo_realm_id="9341457877206856",
        qbo_environment=environment,
    )
    client = QboClient(
        settings=settings,
        tokens=StubTokens(),  # type: ignore[arg-type]
        http=httpx.Client(transport=transport),
        sleep=slept.append,
    )
    return client, transport, slept


# --- addressing -------------------------------------------------------------


def test_the_sandbox_base_url_is_company_scoped() -> None:
    client, _, _ = _client([])
    assert client.base_url == f"{SANDBOX_BASE_URL}/9341457877206856"


def test_production_uses_the_other_host() -> None:
    client, _, _ = _client([], environment="production")
    assert client.base_url == f"{PRODUCTION_BASE_URL}/9341457877206856"


def test_every_request_pins_the_minor_version() -> None:
    """Below 75 Intuit ignores the parameter, so leaving it off is not neutral."""
    client, transport, _ = _client([httpx.Response(200, json={})])
    client.get("purchaseorder", "42")
    assert transport.requests[0].url.params["minorversion"] == MINOR_VERSION


def test_get_addresses_the_entity_and_id() -> None:
    client, transport, _ = _client([httpx.Response(200, json={})])
    client.get("PurchaseOrder", "42")
    assert transport.requests[0].url.path.endswith("/purchaseorder/42")


def test_query_sends_the_statement_as_a_parameter() -> None:
    client, transport, _ = _client([httpx.Response(200, json={})])
    client.query("SELECT Id FROM Vendor WHERE DisplayName = 'Acme'")
    request = transport.requests[0]
    assert request.url.path.endswith("/query")
    assert request.url.params["query"].startswith("SELECT Id FROM Vendor")


def test_create_posts_the_payload() -> None:
    client, transport, _ = _client([httpx.Response(200, json={"Vendor": {"Id": "7"}})])
    result = client.create("vendor", {"DisplayName": "Acme"})
    request = transport.requests[0]
    assert request.method == "POST"
    assert b"Acme" in request.content
    assert result["Vendor"]["Id"] == "7"


def test_there_is_no_update_or_delete() -> None:
    """The client cannot express what the system must never do."""
    client, _, _ = _client([])
    assert not hasattr(client, "update")
    assert not hasattr(client, "delete")


def test_the_bearer_token_is_attached() -> None:
    client, transport, _ = _client([httpx.Response(200, json={})])
    client.get("vendor", "1")
    assert transport.requests[0].headers["Authorization"] == "Bearer access-token-123"


# --- retries ----------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_throttling_and_server_faults_are_retried(status: int) -> None:
    client, transport, slept = _client(
        [httpx.Response(status), httpx.Response(200, json={"ok": True})]
    )
    assert client.get("vendor", "1") == {"ok": True}
    assert len(transport.requests) == 2
    assert slept


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_request_errors_are_not_retried(status: int) -> None:
    """Retrying a 400 re-sends the same bad request; retrying a 401 burns quota."""
    client, transport, _ = _client([httpx.Response(status, json={})])
    with pytest.raises(QboError) as caught:
        client.get("vendor", "1")
    assert caught.value.status_code == status
    assert len(transport.requests) == 1


def test_it_gives_up_after_the_attempt_budget() -> None:
    client, transport, _ = _client([httpx.Response(503) for _ in range(MAX_ATTEMPTS)])
    with pytest.raises(QboError):
        client.get("vendor", "1")
    assert len(transport.requests) == MAX_ATTEMPTS


def test_retry_after_is_honoured() -> None:
    client, _, slept = _client(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    client.get("vendor", "1")
    assert 7.0 <= slept[0] <= 7.5


def test_backoff_grows_between_attempts() -> None:
    client, _, slept = _client(
        [httpx.Response(503), httpx.Response(503), httpx.Response(200, json={})]
    )
    client.get("vendor", "1")
    assert slept[1] > slept[0]


def test_a_transport_failure_is_retried_then_reported() -> None:
    class Broken(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            del request
            raise httpx.ConnectError("no route to host")

    settings = Settings(_env_file=None, qbo_realm_id="1")  # type: ignore[call-arg]
    slept: list[float] = []
    client = QboClient(
        settings=settings,
        tokens=StubTokens(),  # type: ignore[arg-type]
        http=httpx.Client(transport=Broken()),
        sleep=slept.append,
    )
    with pytest.raises(QboError, match="unreachable after"):
        client.get("vendor", "1")
    assert len(slept) == MAX_ATTEMPTS - 1


# --- what it will not do ----------------------------------------------------


def test_no_realm_id_is_refused_up_front() -> None:
    with pytest.raises(QboError, match="QBO_REALM_ID"):
        QboClient(settings=Settings(_env_file=None, qbo_realm_id=""))  # type: ignore[call-arg]


def test_an_error_reports_the_fault_not_the_token() -> None:
    body: dict[str, Any] = {
        "Fault": {"Error": [{"code": "6240", "Message": "Duplicate Name Exists"}]}
    }
    client, _, _ = _client([httpx.Response(400, json=body)])
    with pytest.raises(QboError) as caught:
        client.create("vendor", {"DisplayName": "Acme"})
    text = str(caught.value)
    assert "6240" in text
    assert "Duplicate Name Exists" in text
    assert "access-token-123" not in text
