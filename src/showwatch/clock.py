"""The only module allowed to read real time or really sleep (SPEC §5, enforced by T0.5).

Everything else receives a `Clock`, so tests can drive virtual time with `FakeClock` instead.
"""

import asyncio
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a tz-aware UTC datetime."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for `seconds` (virtual time in tests, real time in production)."""
        ...


class SystemClock:
    """Real wall-clock time, used in production."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        # A negative delay means "already due". Production treats it as zero rather than crash a
        # long-running loop. FakeClock raises instead, so tests catch the scheduling bug.
        await asyncio.sleep(max(0.0, seconds))
