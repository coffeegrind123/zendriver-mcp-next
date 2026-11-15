"""How fill_form ranks names against the words it was given.

The same rules run in JavaScript to find fields by label (_fill_js.tier) and in
Python to pick options (fill.tier); these pin the Python twin.
"""

from __future__ import annotations

import pytest

from zendriver_mcp.errors import InvalidArgumentError
from zendriver_mcp.tools.fill import FieldSpec, _as_bool, _same_time, _validate, best_matches, tier


@pytest.mark.parametrize(
    ("desc", "needle", "expected"),
    [
        ("Email", "email", 4),
        ("Email *", "email", 4),  # required-field markers and colons are not part of the name
        ("Country:", "country", 4),
        ("Email address", "email", 3),
        ("Your email address", "email", 2),
        ("Emailing list", "email", 1),
        ("Phone", "email", 0),
        ("", "email", 0),
    ],
)
def test_tiers(desc: str, needle: str, expected: int) -> None:
    assert tier(desc, needle) == expected


def test_exact_beats_a_longer_name_containing_it() -> None:
    top, idx = best_matches("Large", [["Small"], ["Large"], ["Extra large"]])
    assert (top, idx) == (4, [1])


def test_a_tie_returns_every_tied_index() -> None:
    _, idx = best_matches("email", [["Email address"], ["Email confirmation"], ["Phone"]])
    assert idx == [0, 1]


def test_no_match_returns_nothing() -> None:
    assert best_matches("Mars", [["Earth"], ["Venus"]]) == (0, [])


def test_option_value_counts_as_a_name() -> None:
    assert best_matches("fi", [["Denmark", "dk"], ["Finland", "fi"]]) == (4, [1])


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), ("yes", True), ("Off", False), ("0", False), ("maybe", None), (None, None)],
)
def test_checkbox_values(value: object, expected: bool | None) -> None:
    assert _as_bool(value) is expected


def test_time_values_ignore_seconds_chrome_adds() -> None:
    assert _same_time("07:45", "07:45")
    assert _same_time("07:45:00", "07:45")
    assert not _same_time("17:45", "07:45")
    assert not _same_time("", "07:45")


def test_a_field_needs_one_locator_and_something_to_fill() -> None:
    with pytest.raises(InvalidArgumentError):
        _validate(FieldSpec(value="x"))
    with pytest.raises(InvalidArgumentError):
        _validate(FieldSpec(target="#a", label="A", value="x"))
    with pytest.raises(InvalidArgumentError):
        _validate(FieldSpec(label="A"))
    _validate(FieldSpec(label="A", checked=True))
