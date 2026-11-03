"""Phase 4: poll interval maths (SPEC §8 polling; tests T4.5-T4.7 at the pure level).

Hot windows are wall-clock times in the config timezone, start included, end excluded.
"""

import random
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from showwatch.config import Polling
from showwatch.ratelimit import Backoff
from showwatch.watcher.scheduler import in_hot_window, next_delay

IST = ZoneInfo("Asia/Kolkata")
TUESDAY = date(2026, 11, 3)  # FakeClock's default day


def at(hhmm: str, day: Any = TUESDAY) -> datetime:
    hours, minutes = (int(p) for p in hhmm.split(":")[:2])
    seconds = int(hhmm.split(":")[2]) if hhmm.count(":") == 2 else 0
    return datetime.combine(day, time(hours, minutes, seconds), tzinfo=IST).astimezone(UTC)


def polling(**overrides: Any) -> Polling:
    data: dict[str, Any] = {
        "base_interval_s": 900,
        "hot_interval_s": 60,
        "jitter_pct": 0,
        "hot_windows": [
            {
                "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                "start": "08:00",
                "end": "13:00",
            }
        ],
    }
    data.update(overrides)
    return Polling.model_validate(data)


def delay(now: datetime, p: Polling | None = None, multiplier: int = 1, seed: int = 0) -> float:
    return next_delay(now, p or polling(), IST, random.Random(seed), multiplier=multiplier)


# --- T4.5 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("09:00", 60),  # inside the hot window
        ("08:00", 60),  # start is included
        ("12:59", 60),
        ("13:00", 900),  # end is excluded
        ("14:00", 900),  # outside: base interval
    ],
)
def test_t4_5_hot_window_uses_the_hot_interval(hhmm: str, expected: float) -> None:
    assert delay(at(hhmm)) == expected


def test_t4_5_wakes_at_the_start_of_the_next_hot_window() -> None:
    # 07:50 + base 900 s would be 08:05; wake at 08:00 instead (P4.3).
    assert delay(at("07:50")) == 600


def test_t4_5_never_below_the_60_s_floor() -> None:
    assert delay(at("07:59:30")) == 60  # the window starts in 30 s
    assert delay(at("07:59:59")) == 60


def test_t4_5_hot_window_only_on_its_days() -> None:
    mondays = polling(hot_windows=[{"days": ["Mon"], "start": "08:00", "end": "13:00"}])
    assert not in_hot_window(at("09:00"), mondays, IST)  # a Tuesday
    assert delay(at("09:00"), mondays) == 900
    assert in_hot_window(at("09:00", TUESDAY - timedelta(days=1)), mondays, IST)


def test_t4_5_no_hot_windows_means_the_base_interval() -> None:
    assert delay(at("09:00"), polling(hot_windows=[])) == 900


# --- T4.6 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("hhmm", "nominal"), [("09:00", 60), ("14:00", 900)])
def test_t4_6_jitter_stays_within_bounds_and_above_the_floor(hhmm: str, nominal: float) -> None:
    jittery = polling(jitter_pct=20)
    rng = random.Random(46)
    samples = [next_delay(at(hhmm), jittery, IST, rng) for _ in range(1000)]
    assert all(nominal * 0.8 <= s <= nominal * 1.2 for s in samples)
    assert all(s >= 60 for s in samples)
    assert max(samples) - min(samples) > nominal * 0.1  # jitter is really applied


# --- T4.7 ------------------------------------------------------------------------------------


def test_t4_7_backoff_doubles_and_is_capped_at_60_minutes() -> None:
    backoff = Backoff()
    delays = []
    for _ in range(5):
        backoff.failure()
        delays.append(delay(at("14:00"), multiplier=backoff.multiplier))
    assert delays == [1800, 3600, 3600, 3600, 3600]


def test_t4_7_backoff_resets_after_three_consecutive_successes() -> None:
    backoff = Backoff()
    for _ in range(3):
        backoff.failure()
    backoff.success()
    backoff.success()
    assert backoff.multiplier == 8  # still backing off after two successes
    backoff.failure()  # a failure breaks the streak
    for _ in range(2):
        backoff.success()
    assert backoff.multiplier == 16
    backoff.success()
    assert backoff.multiplier == 1


def test_backoff_never_shortens_a_long_base_interval() -> None:
    long_base = polling(base_interval_s=7200, hot_windows=[])
    assert delay(at("14:00"), long_base, multiplier=2) == 7200


def test_no_early_hot_window_wake_while_backing_off() -> None:
    # Being rate limited outweighs catching the window early.
    assert delay(at("07:50"), multiplier=2) == 1800
