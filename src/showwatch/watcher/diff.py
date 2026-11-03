"""Change detection (SPEC §7 `diff`). Pure: no I/O and no clock (SPEC §5).

* Shows are identified by `show_id`: a show whose price or other details change is not new.
* A date counts as open only once it lists at least one show.
* Shows or dates that disappear are not changes. `disappeared()` lists them for logging.

The watcher diffs against `merge_seen()`, the union of everything seen so far, rather than only the
previous poll. A listing that flickers away for one poll and comes back is therefore not news, and
after a hold expires the same shows never trigger a second hold (docs/DESIGN-NOTES.md P4.2, P4.7).
"""

from collections.abc import Iterable, Mapping
from datetime import date
from zoneinfo import ZoneInfo

from showwatch.config import Target
from showwatch.matcher import rank_shows
from showwatch.models import Change, ChangeKind, Show, Snapshot


def _ordered(shows: Iterable[Show]) -> tuple[Show, ...]:
    return tuple(sorted(shows, key=lambda s: (s.starts_at, s.show_id)))


def _open(snapshot: Snapshot) -> Mapping[date, tuple[Show, ...]]:
    return {day: shows for day, shows in snapshot.open_dates.items() if shows}


def diff(prev: Snapshot | None, curr: Snapshot) -> list[Change]:
    """What is new in `curr` compared with `prev`, by date. The first snapshot is a baseline."""
    if prev is None:
        return []
    before = _open(prev)
    changes: list[Change] = []
    for day in sorted(_open(curr)):
        shows = curr.open_dates[day]
        if day not in before:
            changes.append(Change(ChangeKind.DATE_OPENED, curr.target_id, day, _ordered(shows)))
            continue
        known = {s.show_id for s in before[day]}
        added = [s for s in shows if s.show_id not in known]
        if added:
            changes.append(Change(ChangeKind.SHOW_ADDED, curr.target_id, day, _ordered(added)))
    return changes


def disappeared(prev: Snapshot | None, curr: Snapshot) -> list[Show]:
    """Shows in `prev` that `curr` no longer lists (for logging only)."""
    if prev is None:
        return []
    now_listed = {(day, s.show_id) for day, shows in curr.open_dates.items() for s in shows}
    return [
        s
        for day, shows in prev.open_dates.items()
        for s in shows
        if (day, s.show_id) not in now_listed
    ]


def merge_seen(seen: Snapshot | None, curr: Snapshot) -> Snapshot:
    """Everything ever seen, by date and show_id. Latest details win; empty dates are dropped."""
    merged: dict[date, dict[str, Show]] = {}
    for snapshot in (seen, curr):
        if snapshot is None:
            continue
        for day, shows in snapshot.open_dates.items():
            for s in shows:
                merged.setdefault(day, {})[s.show_id] = s
    return Snapshot(
        curr.target_id,
        curr.taken_at,
        {day: _ordered(by_id.values()) for day, by_id in sorted(merged.items()) if by_id},
    )


def relevant_view(snapshot: Snapshot, target: Target, *, tz: ZoneInfo) -> Snapshot:
    """Only the target's dates, and only shows the target would accept (`rank_shows`)."""
    wanted = set(target.dates)
    kept: dict[date, tuple[Show, ...]] = {}
    for day, shows in snapshot.open_dates.items():
        if day in wanted:
            eligible = rank_shows(shows, target, tz=tz)
            if eligible:
                kept[day] = _ordered(eligible)
    return Snapshot(snapshot.target_id, snapshot.taken_at, kept)
