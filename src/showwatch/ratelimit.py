"""Polite traffic (C4): one global request gate, and exponential backoff.

`PollGate.poll()` wraps every availability check. It holds a lock for the whole request, so no two
requests are ever in flight (single-flight), and it starts a request only once
`GLOBAL_MIN_SPACING_S` has passed since the previous one started, across all targets.
`PollGate.exclusive()` lets a booking run (Phase 7) hold the gate, so polling pauses meanwhile
(docs/DESIGN-NOTES.md P4.6). `PollGate.pace()` spaces an extra request inside one check, for a
site that needs one page per date (Phase 10).

Spacing waits use the injected Clock. Because the lock is held while waiting, only one task is ever
asleep in the gate, which keeps FakeClock's virtual time exact.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from showwatch import limits
from showwatch.clock import Clock


class PollGate:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_start: datetime | None = None

    @asynccontextmanager
    async def poll(self) -> AsyncIterator[None]:
        async with self._lock:
            await self._space()
            yield

    async def pace(self) -> None:
        """Space one more request inside the current `poll()` (P6.4, P10.3).

        For an adapter whose check needs several pages (one per date). The lock is already held,
        so the extra request is still single-flight, and it starts no sooner than
        GLOBAL_MIN_SPACING_S after the previous one. The next poll is spaced from it in turn.
        """
        if not self._lock.locked():
            raise RuntimeError("pace() is only for an extra request inside poll()")
        await self._space()

    async def _space(self) -> None:
        if self._last_start is not None:
            elapsed = (self._clock.now() - self._last_start).total_seconds()
            wait = limits.GLOBAL_MIN_SPACING_S - elapsed
            if wait > 0:
                await self._clock.sleep(wait)
        self._last_start = self._clock.now()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Hold the gate for a whole session (a booking run). Polls wait until it ends."""
        async with self._lock:
            yield


class Backoff:
    """Doubles on each failure; resets after RESET_AFTER consecutive successes (T4.7)."""

    RESET_AFTER = 3
    _MAX_LEVEL = 12  # 4096x; the interval itself is capped by the scheduler

    def __init__(self) -> None:
        self.level = 0
        self._streak = 0

    @property
    def multiplier(self) -> int:
        return 1 << self.level

    def failure(self) -> None:
        self.level = min(self.level + 1, self._MAX_LEVEL)
        self._streak = 0

    def success(self) -> None:
        if self.level == 0:
            return
        self._streak += 1
        if self._streak >= self.RESET_AFTER:
            self.level = 0
            self._streak = 0
