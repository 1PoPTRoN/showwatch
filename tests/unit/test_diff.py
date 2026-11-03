"""Phase 4: the pure change detector (SPEC §7 `diff`; tests T4.1-T4.4 at the pure level).

Shows are identified by `show_id`. A date counts as open only once it has at least one show.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from showwatch.models import Change, ChangeKind, Show, Snapshot
from showwatch.watcher.diff import diff, disappeared, merge_seen, relevant_view
from tests.support.shows import FRI, SAT, show
from tests.support.targets import make_target

AT = datetime(2026, 11, 3, 3, 30, tzinfo=UTC)
A, B, C = show("a", at="19:00"), show("b", at="20:00"), show("c", at="18:30")


def snap(open_dates: Mapping[date, Sequence[Show]], target_id: str = "t") -> Snapshot:
    return Snapshot(target_id, AT, {d: tuple(s) for d, s in open_dates.items()})


def test_t4_1_first_snapshot_is_only_a_baseline() -> None:
    assert diff(None, snap({FRI: [A, B]})) == []


def test_t4_2_date_opening_is_one_change_and_repeats_are_silent() -> None:
    closed, opened = snap({}), snap({FRI: [B, A]})
    assert diff(closed, opened) == [Change(ChangeKind.DATE_OPENED, "t", FRI, (A, B))]
    assert diff(opened, opened) == []


def test_t4_3_new_show_on_an_open_date_carries_only_the_new_show() -> None:
    assert diff(snap({FRI: [A]}), snap({FRI: [A, B]})) == [
        Change(ChangeKind.SHOW_ADDED, "t", FRI, (B,))
    ]


def test_t4_4_disappearing_show_or_date_is_not_a_change() -> None:
    assert diff(snap({FRI: [A, B]}), snap({FRI: [A]})) == []
    assert diff(snap({FRI: [A], SAT: [C]}), snap({FRI: [A]})) == []


def test_t4_4_disappeared_lists_what_went_away() -> None:
    prev, curr = snap({FRI: [A, B], SAT: [C]}), snap({FRI: [A]})
    assert {s.show_id for s in disappeared(prev, curr)} == {"b", "c"}
    assert disappeared(None, curr) == []


def test_shows_are_identified_by_id_not_by_every_field() -> None:
    # A price change is not a new show.
    cheaper = show("a", at="19:00", price=300)
    assert diff(snap({FRI: [A]}), snap({FRI: [cheaper]})) == []


def test_a_date_without_shows_is_not_open() -> None:
    empty, first = snap({FRI: []}), snap({FRI: [A]})
    assert diff(snap({}), empty) == []
    assert diff(empty, first) == [Change(ChangeKind.DATE_OPENED, "t", FRI, (A,))]


def test_several_changes_are_ordered_by_date_then_shows_by_time() -> None:
    sat_show = show("s", on=SAT)
    changes = diff(snap({FRI: [A]}), snap({SAT: [sat_show], FRI: [A, B, C]}))
    assert changes == [
        Change(ChangeKind.SHOW_ADDED, "t", FRI, (C, B)),  # 18:30 before 20:00
        Change(ChangeKind.DATE_OPENED, "t", SAT, (sat_show,)),
    ]


def test_merge_seen_remembers_everything_ever_seen() -> None:
    seen = merge_seen(None, snap({FRI: [A, B]}))
    seen = merge_seen(seen, snap({FRI: [A]}))  # B vanished for one poll ...
    assert diff(seen, snap({FRI: [A, B]})) == []  # ... and coming back is not news (P4.2)
    assert {s.show_id for s in seen.open_dates[FRI]} == {"a", "b"}


def test_merge_seen_keeps_the_latest_details_and_drops_empty_dates() -> None:
    newer = show("a", at="19:00", price=300)
    seen = merge_seen(snap({FRI: [A]}), snap({FRI: [newer], SAT: []}))
    assert seen.open_dates[FRI] == (newer,)
    assert SAT not in seen.open_dates


def test_relevant_view_keeps_only_the_targets_dates_and_eligible_shows() -> None:
    target = make_target()  # watches FRI only; Cinema A/B; IMAX 2D/4DX/2D; 18:00-22:30
    elsewhere = show("elsewhere", venue="Cinema Z")
    too_late = show("late", at="23:00")
    view = relevant_view(
        snap({FRI: [A, elsewhere, too_late], SAT: [show("sat", on=SAT)]}),
        target,
        tz=ZoneInfo("Asia/Kolkata"),
    )
    assert dict(view.open_dates) == {FRI: (A,)}


_SHOW_POOL = [show(f"s{i}", at=f"{18 + i % 4}:{(i * 7) % 60:02d}") for i in range(8)]
_dates = st.sampled_from([FRI, SAT])
_open = st.dictionaries(_dates, st.lists(st.sampled_from(_SHOW_POOL), unique=True, max_size=5))


@given(_open, _open)
def test_changes_only_ever_contain_new_shows(
    prev_dates: dict[date, list[Show]], curr_dates: dict[date, list[Show]]
) -> None:
    prev, curr = snap(prev_dates), snap(curr_dates)
    seen_before = {(d, s.show_id) for d, shows in prev.open_dates.items() for s in shows}
    for change in diff(prev, curr):
        assert change.shows
        for s in change.shows:
            assert (change.on_date, s.show_id) not in seen_before
            assert s in curr.open_dates[change.on_date]
    assert diff(curr, curr) == []
