"""LegiScan numeric status codes -> the SPEC Phase 1 public status ladder.

Public ladder (SPEC section 6, Phase 1 item 2):
    Introduced -> Committee -> Passed chamber -> Passed both -> Signed/Vetoed -> Dead

LegiScan source codes (LegiScan API manual, BillStatus enum):
    0 = Not introduced (prefiled / draft)
    1 = Introduced
    2 = Engrossed   (passed its chamber of origin, in the form as passed)
    3 = Enrolled    (passed both chambers, sent to the governor)
    4 = Passed      (terminal success: signed / became law / chaptered)
    5 = Vetoed
    6 = Failed      ("limited support based on state" per the manual)

Conservative mapping notes -- read before "fixing" these:
* There is NO LegiScan status code for "in committee". A bill coded 1 may
  sit in committee for months. The code alone cannot place a bill on the
  Committee rung, so code 1 maps to Introduced. Refining to Committee
  requires the bill's history/progress events (Phase 1 detail pass), not
  the status code, and this module deliberately does not guess it.
* Code 0 (not yet introduced) also maps to Introduced, the first rung; it
  means the bill is at the very start of the ladder, not that it moved.
* Code 4 "Passed": LegiScan uses it for the terminal enacted state
  (e.g. a bill whose last action is a chaptered session law). It maps to
  the Signed rung. Where a state distinguishes adoption of a resolution
  from a signature, the detail record -- not this code -- carries that.
* Code 5 maps to the Vetoed half of the Signed/Vetoed rung.
* Unknown / missing codes map to PublicStatus.UNKNOWN, never to a guessed
  rung. UNKNOWN is not part of the public ladder; it exists so bad source
  data is visible instead of silently relabelled.
"""

from __future__ import annotations

from enum import StrEnum


class PublicStatus(StrEnum):
    """The public status ladder shown to visitors, plus UNKNOWN."""

    INTRODUCED = "Introduced"
    COMMITTEE = "Committee"
    PASSED_CHAMBER = "Passed chamber"
    PASSED_BOTH = "Passed both"
    SIGNED = "Signed"
    VETOED = "Vetoed"
    DEAD = "Dead"
    UNKNOWN = "Unknown"


#: LegiScan's own names for its numeric status codes.
LEGISCAN_STATUS_NAMES: dict[int, str] = {
    0: "Not introduced",
    1: "Introduced",
    2: "Engrossed",
    3: "Enrolled",
    4: "Passed",
    5: "Vetoed",
    6: "Failed",
}

_MAP: dict[int, PublicStatus] = {
    0: PublicStatus.INTRODUCED,  # prefiled: start of ladder, see module docstring
    1: PublicStatus.INTRODUCED,  # committee stage is not encoded in the status code
    2: PublicStatus.PASSED_CHAMBER,  # Engrossed = passed chamber of origin
    3: PublicStatus.PASSED_BOTH,  # Enrolled = passed both, to the governor
    4: PublicStatus.SIGNED,  # Passed = terminal enacted state
    5: PublicStatus.VETOED,
    6: PublicStatus.DEAD,  # Failed
}


def legiscan_status_name(code: int | str | None) -> str | None:
    """Return LegiScan's name for a status code, or None if unrecognised."""
    parsed = _parse(code)
    if parsed is None:
        return None
    return LEGISCAN_STATUS_NAMES.get(parsed)


def map_legiscan_status(code: int | str | None) -> PublicStatus:
    """Map a LegiScan numeric status code onto the public ladder.

    Accepts the int or numeric-string forms LegiScan returns. Anything
    outside the documented 0-6 range (including None) returns
    PublicStatus.UNKNOWN rather than a guess.
    """
    parsed = _parse(code)
    if parsed is None:
        return PublicStatus.UNKNOWN
    return _MAP.get(parsed, PublicStatus.UNKNOWN)


def _parse(code: int | str | None) -> int | None:
    if code is None or isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        try:
            return int(code.strip())
        except ValueError:
            return None
    return None
