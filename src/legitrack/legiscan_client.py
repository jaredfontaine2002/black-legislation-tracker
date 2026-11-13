"""LegiScan API client (primary ingestion feed, SPEC section 3).

Validated live on 2026-11-13 (see PROOF-PULL-NC-2026-11-13.md):
* Base URL https://api.legiscan.com/ with ?key=...&op=... query params.
* The API sits behind Cloudflare: the default Python urllib User-Agent
  gets a 403, so every request sends a browser User-Agent header.
* getMasterList takes the session id in a parameter named ``id``
  (NOT ``session``); getSessionList takes ``state``; getBill takes ``id``.

The API key is read from the LEGISCAN_API_KEY environment variable only.
It is never hardcoded, never written to disk by this module, and never
included in exception messages or logs (error messages name the operation,
not the URL, because the URL contains the key).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

BASE_URL = "https://api.legiscan.com/"
ENV_VAR = "LEGISCAN_API_KEY"
# Browser User-Agent: the default Python UA is rejected with 403 (proof pull).
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 LegiTrack/0.1"
)

# Transport hook for tests: (url, headers) -> decoded JSON payload.
Transport = Callable[[str, dict[str, str]], dict[str, Any]]


class LegiScanError(RuntimeError):
    """A LegiScan call failed. Messages never contain the API key."""


class MissingApiKeyError(LegiScanError):
    """LEGISCAN_API_KEY was not present in the environment."""


@dataclass(frozen=True)
class Session:
    """One row of a getSessionList response."""

    session_id: int
    state_abbr: str
    year_start: int | None
    year_end: int | None
    session_name: str
    session_tag: str = ""
    special: bool = False
    sine_die: bool = False
    dataset_hash: str = ""


@dataclass(frozen=True)
class MasterBill:
    """One bill row of a getMasterList response (the delta-pull unit)."""

    bill_id: int
    number: str
    change_hash: str
    status: int | None
    title: str = ""
    description: str = ""
    status_date: str = ""
    last_action: str = ""
    last_action_date: str = ""
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def parse_session_list(payload: dict[str, Any]) -> list[Session]:
    """Parse a getSessionList payload into Session rows."""
    _require_ok(payload, "getSessionList")
    sessions: list[Session] = []
    for row in payload.get("sessions", []):
        sessions.append(
            Session(
                session_id=int(row["session_id"]),
                state_abbr=str(row.get("state_abbr", "")),
                year_start=_as_int(row.get("year_start")),
                year_end=_as_int(row.get("year_end")),
                session_name=str(row.get("session_name") or row.get("name") or ""),
                session_tag=str(row.get("session_tag", "")),
                special=bool(row.get("special", 0)),
                sine_die=bool(row.get("sine_die", 0)),
                dataset_hash=str(row.get("dataset_hash") or row.get("session_hash") or ""),
            )
        )
    return sessions


def parse_master_list(payload: dict[str, Any]) -> list[MasterBill]:
    """Parse a getMasterList payload into MasterBill rows.

    The payload's ``masterlist`` object mixes one ``session`` entry with
    numerically-keyed bill rows; the session entry is skipped here.
    """
    _require_ok(payload, "getMasterList")
    masterlist = payload.get("masterlist")
    if not isinstance(masterlist, dict):
        raise LegiScanError("getMasterList response had no 'masterlist' object")
    bills: list[MasterBill] = []
    for key, row in masterlist.items():
        if key == "session" or not isinstance(row, dict):
            continue
        bills.append(
            MasterBill(
                bill_id=int(row["bill_id"]),
                number=str(row.get("number", "")),
                change_hash=str(row.get("change_hash", "")),
                status=_as_int(row.get("status")),
                title=str(row.get("title", "")),
                description=str(row.get("description", "")),
                status_date=str(row.get("status_date", "")),
                last_action=str(row.get("last_action", "")),
                last_action_date=str(row.get("last_action_date", "")),
                url=str(row.get("url", "")),
                raw=dict(row),
            )
        )
    return bills


class LegiScanClient:
    """Minimal typed client for the three Phase 1 LegiScan operations."""

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
                f"{ENV_VAR} is not set; export it in the environment before running ingestion"
            )
        self._api_key = key.strip()
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport or self._urllib_transport
        self.queries_used = 0

    # -- public operations -------------------------------------------------

    def get_session_list(self, state: str) -> list[Session]:
        """getSessionList for a two-letter state abbreviation (e.g. 'NC')."""
        payload = self._call("getSessionList", state=state.upper())
        return parse_session_list(payload)

    def get_master_list(self, session_id: int) -> list[MasterBill]:
        """getMasterList for a session. NOTE: the parameter is named ``id``."""
        payload = self._call("getMasterList", id=int(session_id))
        return parse_master_list(payload)

    def get_bill(self, bill_id: int) -> dict[str, Any]:
        """getBill detail for one bill; returns the ``bill`` object as a dict."""
        payload = self._call("getBill", id=int(bill_id))
        _require_ok(payload, "getBill")
        bill = payload.get("bill")
        if not isinstance(bill, dict):
            raise LegiScanError("getBill response had no 'bill' object")
        return bill

    # -- internals -----------------------------------------------------------

    def _call(self, op: str, **params: Any) -> dict[str, Any]:
        query = {"key": self._api_key, "op": op}
        query.update(params)
        url = self._base_url + "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        self.queries_used += 1
        try:
            return self._transport(url, headers)
        except LegiScanError:
            raise
        except Exception as exc:  # transport errors: re-raise without the URL/key
            raise LegiScanError(f"LegiScan operation {op} failed: {type(exc).__name__}") from exc

    def _urllib_transport(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Message names only the status code: the URL carries the key.
            raise LegiScanError(f"LegiScan HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LegiScanError(f"LegiScan connection error: {exc.reason}") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LegiScanError("LegiScan returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise LegiScanError("LegiScan returned an unexpected payload shape")
        return payload


def _require_ok(payload: dict[str, Any], op: str) -> None:
    status = payload.get("status")
    if status != "OK":
        alert = payload.get("alert") or {}
        message = alert.get("message") if isinstance(alert, dict) else None
        raise LegiScanError(f"LegiScan operation {op} returned status={status!r} {message or ''}".strip())


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
