"""Phase 2: `rank_shows` filtering and ordering (SPEC §7 "Show ranking", tests T2.10-T2.15).

Show times are written in IST and stored in UTC, as the store will hold them. A filter that
compares UTC wall-clock times would therefore fail T2.10 immediately.
"""

import random
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from showwatch.matcher import rank_shows
from showwatch.models import Show
from tests.support.targets import make_target

IST = ZoneInfo("Asia/Kolkata")
ON = date(2026, 12, 18)


def show(
    show_id: str,
    *,
    venue: str = "Cinema A",
    fmt: str = "IMAX 2D",
    lang: str = "hindi",
    at: str = "19:00",
    price: int = 400,
) -> Show:
    hours, minutes = (int(part) for part in at.split(":"))
    starts = datetime.combine(ON, time(hours, minutes), tzinfo=IST).astimezone(UTC)
    return Show(show_id, venue, fmt, lang, starts, price, f"http://127.0.0.1/show/{show_id}")


def ids(shows: list[Show]) -> list[str]:
    return [s.show_id for s in shows]


TARGET = make_target()  # hindi; IMAX 2D > 4DX > 2D; Cinema A > Cinema B; 18:00-22:30; <= Rs 600


# --- filters -------------------------------------------------------------------------------


def test_t2_10_window_bounds_are_inclusive_in_local_time() -> None:
    shows = [show(f"at-{t}", at=t) for t in ("17:59", "18:00", "20:00", "22:30", "22:31")]
    assert ids(rank_shows(shows, TARGET)) == ["at-18:00", "at-20:00", "at-22:30"]


def test_t2_10_window_uses_the_given_timezone() -> None:
    # 18:00 IST is 12:30 UTC, which is outside the 18:00-22:30 window when read as UTC.
    shows = [show("ist-18", at="18:00")]
    assert ids(rank_shows(shows, TARGET)) == ["ist-18"]
    assert rank_shows(shows, TARGET, tz=ZoneInfo("UTC")) == []


@pytest.mark.parametrize(
    "rejected",
    [
        pytest.param(show("tamil", lang="tamil"), id="other-language"),
        pytest.param(show("3d", fmt="3D"), id="format-not-listed"),
        pytest.param(show("other-venue", venue="Cinema Z"), id="venue-not-listed"),
    ],
)
def test_t2_11_wrong_language_format_or_venue_filtered(rejected: Show) -> None:
    assert rank_shows([rejected, show("ok")], TARGET) == [show("ok")]


def test_labels_match_ignoring_case_and_spacing() -> None:
    relaxed = show("relaxed", venue="cinema  a", fmt="imax 2d", lang="Hindi")
    assert rank_shows([relaxed], TARGET) == [relaxed]


def test_t2_12_price_above_max_filtered_and_at_max_kept() -> None:
    shows = [show("at-max", price=600), show("over", price=601)]
    assert ids(rank_shows(shows, TARGET)) == ["at-max"]


# --- ordering -------------------------------------------------------------------------------

MIXED = [
    show("b-imax-18", venue="Cinema B", fmt="IMAX 2D", at="18:00"),
    show("a-2d-1830", venue="Cinema A", fmt="2D", at="18:30"),
    show("a-imax-21", venue="Cinema A", fmt="IMAX 2D", at="21:00"),
    show("a-imax-19-z", venue="Cinema A", fmt="IMAX 2D", at="19:00"),
    show("a-imax-19-a", venue="Cinema A", fmt="IMAX 2D", at="19:00"),
    show("a-4dx-18", venue="Cinema A", fmt="4DX", at="18:00"),
    show("b-2d-18", venue="Cinema B", fmt="2D", at="18:00"),
]
MIXED_ORDER = [
    "a-imax-19-a",  # best venue, best format, earliest time, then show_id breaks the 19:00 tie
    "a-imax-19-z",
    "a-imax-21",
    "a-4dx-18",  # an earlier show in a worse format comes after
    "a-2d-1830",
    "b-imax-18",  # the second venue only after everything at the first
    "b-2d-18",
]


def test_t2_13_order_is_venue_then_format_then_time_then_id() -> None:
    assert ids(rank_shows(MIXED, TARGET)) == MIXED_ORDER


def test_t2_14_no_venues_means_any_venue_ranked_by_format_then_time() -> None:
    anywhere = make_target(venues=[])
    shows = [*MIXED, show("z-imax-1830", venue="Cinema Z", at="18:30")]
    assert ids(rank_shows(shows, anywhere)) == [
        "b-imax-18",  # IMAX 2D, by time across all venues
        "z-imax-1830",
        "a-imax-19-a",
        "a-imax-19-z",
        "a-imax-21",
        "a-4dx-18",  # then 4DX
        "b-2d-18",  # then 2D
        "a-2d-1830",
    ]


def test_t2_15_input_order_does_not_matter() -> None:
    rng = random.Random(15)
    for _ in range(25):
        shuffled = MIXED.copy()
        rng.shuffle(shuffled)
        assert ids(rank_shows(shuffled, TARGET)) == MIXED_ORDER


@settings(max_examples=100, deadline=None)
@given(st.permutations([*MIXED, show("a-imax-19-a", venue="Cinema A", at="19:00", price=500)]))
def test_t2_15_order_is_total_even_with_duplicate_ids(shows: list[Show]) -> None:
    # Two distinct shows share an id: the order must still not depend on the input order.
    assert rank_shows(shows, TARGET) == rank_shows(sorted(shows, key=repr), TARGET)


def test_input_is_not_modified_and_a_new_list_is_returned() -> None:
    shows = tuple(MIXED)
    result = rank_shows(shows, TARGET)
    assert shows == tuple(MIXED)
    assert isinstance(result, list)
