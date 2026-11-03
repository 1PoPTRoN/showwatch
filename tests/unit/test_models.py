"""Phase 1: invariants of the SPEC §7 domain dataclasses.

The shapes are fixed by §7. These tests pin the extra guarantees that later phases rely on:
everything is immutable, and every datetime is tz-aware.
"""

import dataclasses
from datetime import UTC, date, datetime

import pytest

from showwatch.models import (
    Change,
    ChangeKind,
    SeatBlock,
    SeatMap,
    SeatStatus,
    Show,
    Snapshot,
)

A, T, G = SeatStatus.AVAILABLE, SeatStatus.TAKEN, SeatStatus.GAP
STARTS = datetime(2026, 12, 18, 13, 0, tzinfo=UTC)


def make_show(**overrides: object) -> Show:
    fields: dict[str, object] = {
        "show_id": "s1",
        "venue": "Cinema A",
        "screen_format": "IMAX 2D",
        "language": "hindi",
        "starts_at": STARTS,
        "min_price": 450,
        "url": "http://127.0.0.1/show/s1",
    }
    fields.update(overrides)
    return Show(**fields)  # type: ignore[arg-type]


def test_enum_values_match_spec() -> None:
    assert [s.value for s in SeatStatus] == ["A", "T", "_"]
    assert [k.value for k in ChangeKind] == ["date_opened", "show_added"]


def test_show_requires_tz_aware_start() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        make_show(starts_at=datetime(2026, 12, 18, 18, 30))  # noqa: DTZ001 (deliberately naive)


@pytest.mark.parametrize(("field", "value"), [("min_price", -1), ("show_id", "")])
def test_show_rejects_impossible_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        make_show(**{field: value})


def test_snapshot_requires_tz_aware_time() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        Snapshot("t", datetime(2026, 11, 3, 9, 0), {})  # noqa: DTZ001 (deliberately naive)


def test_snapshot_open_dates_are_a_read_only_copy() -> None:
    source: dict[date, tuple[Show, ...]] = {date(2026, 12, 18): (make_show(),)}
    snap = Snapshot("movie-fri", STARTS, source)

    source[date(2026, 12, 19)] = ()
    assert list(snap.open_dates) == [date(2026, 12, 18)]
    with pytest.raises(TypeError):
        snap.open_dates[date(2026, 12, 20)] = ()  # type: ignore[index]


def test_seat_map_rejects_duplicate_row_labels() -> None:
    with pytest.raises(ValueError, match="duplicate row 'H'"):
        SeatMap(rows=(("H", (A, A)), ("H", (A, T))))


@pytest.mark.parametrize(("start_col", "n"), [(-1, 2), (0, 0)])
def test_seat_block_rejects_impossible_values(start_col: int, n: int) -> None:
    with pytest.raises(ValueError, match="SeatBlock"):
        SeatBlock(row="H", start_col=start_col, n=n)


def test_everything_is_frozen() -> None:
    values: list[object] = [
        make_show(),
        SeatMap(rows=(("H", (A, G, T)),)),
        SeatBlock(row="H", start_col=0, n=1),
        Snapshot("movie-fri", STARTS, {}),
        Change(ChangeKind.DATE_OPENED, "movie-fri", date(2026, 12, 18), (make_show(),)),
    ]
    for value in values:
        field = dataclasses.fields(value)[0].name  # type: ignore[arg-type]
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field, "changed")
