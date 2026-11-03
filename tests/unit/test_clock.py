"""T0.4: FakeClock advances virtual time instantly and records each sleep.

Also covers the production SystemClock, the only place real time is allowed (SPEC §5).
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from showwatch.clock import SystemClock
from tests.support.clock import FAKE_CLOCK_START, FakeClock


async def test_t0_4_sleep_advances_now_without_real_waiting(fake_clock: FakeClock) -> None:
    start = fake_clock.now()
    wall_start = time.perf_counter()

    await fake_clock.sleep(30)

    wall_elapsed = time.perf_counter() - wall_start
    assert fake_clock.now() - start == timedelta(seconds=30)
    assert wall_elapsed < 0.1
    assert fake_clock.sleeps == [30]


async def test_t0_4_sleeps_accumulate_and_are_recorded_in_order(fake_clock: FakeClock) -> None:
    start = fake_clock.now()
    for seconds in (60, 0.5, 0):
        await fake_clock.sleep(seconds)
    assert fake_clock.sleeps == [60, 0.5, 0]
    assert fake_clock.now() - start == timedelta(seconds=60.5)


def test_t0_4_now_is_tz_aware_utc(fake_clock: FakeClock) -> None:
    now = fake_clock.now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_t0_4_default_start_is_pinned() -> None:
    # Pinned so date-dependent tests never turn into time bombs (docs/DESIGN-NOTES.md X1).
    assert FakeClock().now() == FAKE_CLOCK_START == datetime(2026, 11, 3, 3, 30, tzinfo=UTC)


def test_t0_4_advance_moves_time_without_recording_a_sleep(fake_clock: FakeClock) -> None:
    start = fake_clock.now()
    fake_clock.advance(90)
    assert fake_clock.now() - start == timedelta(seconds=90)
    assert fake_clock.sleeps == []


async def test_t0_4_sleep_yields_to_other_tasks(fake_clock: FakeClock) -> None:
    ran: list[str] = []

    async def other() -> None:
        ran.append("other")

    task = asyncio.create_task(other())
    await fake_clock.sleep(1)
    assert ran == ["other"]
    await task


async def test_t0_4_negative_sleep_is_a_bug(fake_clock: FakeClock) -> None:
    # Scheduler maths that produces a negative delay should fail loudly in tests.
    with pytest.raises(ValueError, match="negative"):
        await fake_clock.sleep(-1)


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(datetime(2026, 11, 3, 9, 0), id="naive"),  # noqa: DTZ001 (deliberately naive)
        pytest.param(
            datetime(2026, 11, 3, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            id="ist-offset",
        ),
    ],
)
def test_t0_4_start_must_be_utc(start: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        FakeClock(start)


def test_system_clock_now_is_tz_aware_utc() -> None:
    now = SystemClock().now()
    assert now.utcoffset() == timedelta(0)


async def test_system_clock_sleep_zero_and_negative_return_promptly() -> None:
    # Production never crashes on a negative delay; it treats it as zero.
    clock = SystemClock()
    wall_start = time.perf_counter()
    await clock.sleep(0)
    await clock.sleep(-5)
    assert time.perf_counter() - wall_start < 0.1
