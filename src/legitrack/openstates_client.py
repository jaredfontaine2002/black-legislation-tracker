"""OpenStates API v3 client (cross-check + legislator/contact feed, SPEC section 3).

Validated live on 2026-11-13 (see PROOF-PULL-OPENSTATES-NC-2026-11-13.md):
* Base URL https://v3.openstates.org, key sent as the X-API-KEY header.
* NC checks: /people for a jurisdiction returned 173 legislators with
  published emails and photo URLs; /bills for NC session 2025 returned
  2,338 bills, exactly matching LegiScan.

The API key is read from the OPENSTATES_API_KEY environment variable only.
It is never hardcoded, logged, or included in exception messages.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE_URL = "https://v3.openstates.org"
ENV_VAR = "OPENSTATES_API_KEY"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 LegiTrack/0.1"
)

# Transport hook for tests: (url, headers) -> decoded JSON payload.
Transport = Callable[[str, dict[str, str]], dict[str, Any]]


class OpenStatesError(RuntimeError):
    """An OpenStates call failed. Messages never contain the API key."""


class MissingApiKeyError(OpenStatesError):
    """OPENSTATES_API_KEY was not present in the environment."""


def jurisdiction_id_for_state(state_abbr: str) -> str:
    """OCD jurisdiction id for a state, e.g. NC -> .../state:nc/government."""
    return f"ocd-jurisdiction/country:us/state:{state_abbr.lower()}/government"


class OpenStatesClient:
    """Minimal typed client for the Phase 1 OpenStates list endpoints."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(ENV_VAR, "")
        if not key.strip():
            raise MissingApiKeyError(
                f"{ENV_VAR} is not set; export it in the environment before running"
            )
        self._api_key = key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._urllib_transport
        self.queries_used = 0

    # -- public operations ---------------------------------------------------

    def list_jurisdictions(self, *, page: int = 1, per_page: int = 52) -> dict[str, Any]:
        """GET /jurisdictions -- returns the decoded page payload."""
        return self._get("/jurisdictions", page=page, per_page=per_page)

    def list_people(
        self,
        jurisdiction: str,
        *,
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """GET /people for an OCD jurisdiction id (legislators + contacts)."""
        return self._get("/people", jurisdiction=jurisdiction, page=page, per_page=per_page)

    def list_bills(
        self,
        jurisdiction: str,
        *,
        session: str | None = None,
        page: int = 1,
        per_page: int = 20,
        sort: str = "updated_desc",
    ) -> dict[str, Any]:
        """GET /bills for an OCD jurisdiction id, optionally one session."""
        params: dict[str, Any] = {
            "jurisdiction": jurisdiction,
            "page": page,
            "per_page": per_page,
            "sort": sort,
        }
        if session:
            params["session"] = session
        return self._get("/bills", **params)

    # -- internals -------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        url = self._base_url + path + "?" + urllib.parse.urlencode(clean)
        headers = {
            "X-API-KEY": self._api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        self.queries_used += 1
        try:
            return self._transport(url, headers)
        except OpenStatesError:
            raise
        except Exception as exc:
            raise OpenStatesError(
                f"OpenStates GET {path} failed: {type(exc).__name__}"
            ) from exc

    def _urllib_transport(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise OpenStatesError(f"OpenStates HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OpenStatesError(f"OpenStates connection error: {exc.reason}") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OpenStatesError("OpenStates returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise OpenStatesError("OpenStates returned an unexpected payload shape")
        return payload
