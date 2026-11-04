"""Phase 4: the global request gate (C4: 20 s spacing, single flight; T4.10/T4.11 at unit level)."""

import asyncio
import itertools
from datetime import datetime, timedelta

import pytest

from showwatch.ratelimit import PollGate
from tests.support.clock import FakeClock


async def test_t4_10_request_starts_are_at_least_20_s_apart(fake_clock: FakeClock) -> None:
    gate = PollGate(fake_clock)
    starts: list[datetime] = []
    for _ in range(4):
        async with gate.poll():
            starts.append(fake_clock.now())
            await fake_clock.sleep(3)  # request latency
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(starts)]
    assert gaps == [20, 20, 20]


async def test_spacing_waits_only_for_what_is_left(fake_clock: FakeClock) -> None:
    gate = PollGate(fake_clock)
    async with gate.poll():
        pass
    fake_clock.advance(25)
    before = fake_clock.now()
    async with gate.poll():
        assert fake_clock.now() == before  # 25 s already passed: no wait


async def test_t4_11_concurrent_callers_never_overlap(fake_clock: FakeClock) -> None:
    gate = PollGate(fake_clock)
    in_flight = 0
    peak = 0
    starts: list[datetime] = []

    async def request() -> None:
        nonlocal in_flight, peak
        async with gate.poll():
            in_flight += 1
            peak = max(peak, in_flight)
            starts.append(fake_clock.now())
            for _ in range(3):
                await asyncio.sleep(0)  # give other tasks every chance to barge in
            await fake_clock.sleep(5)
            in_flight -= 1

    await asyncio.gather(*(request() for _ in range(5)))
    assert peak == 1
    assert all(b - a >= timedelta(seconds=20) for a, b in itertools.pairwise(starts))


async def test_exclusive_session_holds_off_polls(fake_clock: FakeClock) -> None:
    # The executor (Phase 7) takes the gate for a whole booking run (docs/DESIGN-NOTES.md P4.6).
    gate = PollGate(fake_clock)
    order: list[str] = []

    async def booking() -> None:
        async with gate.exclusive():
            order.append("booking-start")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            order.append("booking-end")

    async def poll() -> None:
        await asyncio.sleep(0)
        async with gate.poll():
            order.append("poll")

    await asyncio.gather(booking(), poll())
    assert order == ["booking-start", "booking-end", "poll"]


# --- Phase 10: an adapter that needs several requests in one check --------------------------------


async def test_pace_spaces_an_extra_request_inside_a_poll(fake_clock: FakeClock) -> None:
    # P6.4: one availability check may need one page per date. Each extra request goes through
    # the gate, so C4's 20 s spacing holds for it and for the poll that comes after it.
    gate = PollGate(fake_clock)
    starts: list[datetime] = []
    async with gate.poll():
        starts.append(fake_clock.now())
        await fake_clock.sleep(2)
        await gate.pace()
        starts.append(fake_clock.now())
    async with gate.poll():
        starts.append(fake_clock.now())
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(starts)]
    assert gaps == [20, 20]


async def test_pace_outside_a_poll_is_a_bug(fake_clock: FakeClock) -> None:
    gate = PollGate(fake_clock)
    with pytest.raises(RuntimeError, match="inside poll"):
        await gate.pace()
