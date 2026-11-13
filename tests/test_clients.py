"""Tests for the API clients using injected fake transports.

No live API is called, and the only 'keys' here are obvious test
placeholders passed to the constructor in-process -- never real keys,
never read from files.
"""

import urllib.parse

import pytest

from legitrack.legiscan_client import LegiScanClient, LegiScanError, MissingApiKeyError
from legitrack.openstates_client import OpenStatesClient, jurisdiction_id_for_state

TEST_PLACEHOLDER = "test-placeholder-not-a-real-key"


def test_legiscan_missing_env_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEGISCAN_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError):
        LegiScanClient()


def test_legiscan_key_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGISCAN_API_KEY", TEST_PLACEHOLDER)
    seen: dict[str, str] = {}

    def transport(url: str, headers: dict[str, str]) -> dict:
        seen["url"] = url
        seen.update(headers)
        return {"status": "OK", "sessions": []}

    client = LegiScanClient(transport=transport)
    assert client.get_session_list("nc") == []
    params = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert params["op"] == ["getSessionList"]
    assert params["state"] == ["NC"]
    # The key travels in the query string (LegiScan's design)...
    assert params["key"] == [TEST_PLACEHOLDER]
    # ...and a browser User-Agent is always sent (default UA got 403 live).
    assert "Mozilla/5.0" in seen["User-Agent"]


def test_legiscan_master_list_uses_id_param_not_session() -> None:
    seen: dict[str, str] = {}

    def transport(url: str, headers: dict[str, str]) -> dict:
        seen["url"] = url
        return {"status": "OK", "masterlist": {"session": {"session_id": 9999}}}

    client = LegiScanClient(api_key=TEST_PLACEHOLDER, transport=transport)
    assert client.get_master_list(9999) == []
    params = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert params["id"] == ["9999"]
    assert "session" not in params


def test_legiscan_error_status_and_key_not_in_message() -> None:
    def transport(url: str, headers: dict[str, str]) -> dict:
        return {"status": "ERROR", "alert": {"message": "invalid"}}

    client = LegiScanClient(api_key=TEST_PLACEHOLDER, transport=transport)
    with pytest.raises(LegiScanError) as excinfo:
        client.get_session_list("NC")
    assert TEST_PLACEHOLDER not in str(excinfo.value)


def test_legiscan_transport_failure_does_not_leak_key() -> None:
    def transport(url: str, headers: dict[str, str]) -> dict:
        raise ConnectionError(f"boom while calling {url}")  # url contains the key

    client = LegiScanClient(api_key=TEST_PLACEHOLDER, transport=transport)
    with pytest.raises(LegiScanError) as excinfo:
        client.get_session_list("NC")
    assert TEST_PLACEHOLDER not in str(excinfo.value)


def test_openstates_sends_key_as_header_not_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSTATES_API_KEY", TEST_PLACEHOLDER)
    seen: dict[str, str] = {}

    def transport(url: str, headers: dict[str, str]) -> dict:
        seen["url"] = url
        seen.update(headers)
        return {"results": [], "pagination": {}}

    client = OpenStatesClient(transport=transport)
    payload = client.list_people(jurisdiction_id_for_state("NC"))
    assert payload["results"] == []
    assert seen["X-API-KEY"] == TEST_PLACEHOLDER
    assert TEST_PLACEHOLDER not in seen["url"]
    assert "state%3Anc" in seen["url"] or "state:nc" in seen["url"]


def test_openstates_missing_env_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSTATES_API_KEY", raising=False)
    with pytest.raises(Exception, match="OPENSTATES_API_KEY"):
        OpenStatesClient()
