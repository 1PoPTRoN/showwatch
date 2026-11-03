"""Virtual time for tests (SPEC §9 `FakeClock`). Implements `showwatch.clock.Clock`."""

import asyncio
from datetime import UTC, datetime, timedelta

# Tuesday 2026-11-03, 09:00 in Asia/Kolkata. A fixed instant, so date checks never become
# time bombs. Inside the default hot window (08:00-13:00 IST) of the example config.
FAKE_CLOCK_START = datetime(2026, 11, 3, 3, 30, tzinfo=UTC)


class FakeClock:
    """`now()` returns controlled time. `sleep(s)` advances it instantly and records `s`.

    Exact when one task sleeps at a time; ShowWatch runs a single scheduler loop for that reason
    (docs/DESIGN-NOTES.md X2).
    """

    def __init__(self, start: datetime = FAKE_CLOCK_START) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError(f"FakeClock needs a tz-aware UTC start, got {start!r}")
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"negative sleep ({seconds}s) usually means a scheduling bug")
        self.sleeps.append(seconds)
        self._now += timedelta(seconds=seconds)
        await asyncio.sleep(0)  # let other tasks run, as a real sleep would

    def advance(self, seconds: float) -> None:
        """Move time forward from the test itself (not recorded as a sleep)."""
        if seconds < 0:
            raise ValueError("time only moves forward")
        self._now += timedelta(seconds=seconds)
