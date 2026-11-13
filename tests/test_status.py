"""Tests for LegiScan status code -> public ladder mapping."""

import pytest

from legitrack.status import (
    LEGISCAN_STATUS_NAMES,
    PublicStatus,
    legiscan_status_name,
    map_legiscan_status,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, PublicStatus.INTRODUCED),  # not introduced / prefiled: start of ladder
        (1, PublicStatus.INTRODUCED),
        (2, PublicStatus.PASSED_CHAMBER),  # Engrossed
        (3, PublicStatus.PASSED_BOTH),  # Enrolled
        (4, PublicStatus.SIGNED),  # Passed (terminal enacted)
        (5, PublicStatus.VETOED),
        (6, PublicStatus.DEAD),  # Failed
    ],
)
def test_all_documented_codes_map(code: int, expected: PublicStatus) -> None:
    assert map_legiscan_status(code) == expected


def test_numeric_strings_are_accepted() -> None:
    assert map_legiscan_status("2") == PublicStatus.PASSED_CHAMBER
    assert map_legiscan_status(" 4 ") == PublicStatus.SIGNED


@pytest.mark.parametrize("bad", [None, 7, -1, 99, "abc", "", True])
def test_unknown_codes_are_not_guessed(bad: object) -> None:
    assert map_legiscan_status(bad) == PublicStatus.UNKNOWN  # type: ignore[arg-type]


def test_status_code_alone_never_claims_committee() -> None:
    # LegiScan encodes no committee stage in the status code; code 1 must
    # stay Introduced rather than guessing Committee.
    assert map_legiscan_status(1) is not PublicStatus.COMMITTEE


def test_legiscan_names_cover_all_codes() -> None:
    assert LEGISCAN_STATUS_NAMES[1] == "Introduced"
    assert LEGISCAN_STATUS_NAMES[2] == "Engrossed"
    assert LEGISCAN_STATUS_NAMES[3] == "Enrolled"
    assert LEGISCAN_STATUS_NAMES[4] == "Passed"
    assert LEGISCAN_STATUS_NAMES[5] == "Vetoed"
    assert LEGISCAN_STATUS_NAMES[6] == "Failed"
    assert legiscan_status_name(42) is None
    assert legiscan_status_name(None) is None
