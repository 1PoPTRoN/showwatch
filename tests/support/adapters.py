"""A scripted availability source for watcher tests (SPEC §10 Phase 4 `StubAdapter`).

Each target has a script: a list of responses, used in order. The last response repeats once
the script runs out (a steady state). A response is either an `open_dates` mapping, which the
stub wraps in a Snapshot stamped with the current time, or an exception to raise. Every call is
recorded with its start and end times, and so is the most calls ever in flight at once.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from showwatch.config import Target
from showwatch.models import Show, Snapshot
from tests.support.clock import FakeClock

OpenDates = Mapping[date, Sequence[Show]]
Response = OpenDates | BaseException


@dataclass
class Call:
    target_id: str
    started: datetime
    ended: datetime | None = None


class StubAdapter:
    def __init__(
        self,
        clock: FakeClock,
        scripts: Mapping[str, Sequence[Response]],
        *,
        latency_s: float = 0.0,
        on_call: Callable[[Target], Awaitable[None]] | None = None,
    ) -> None:
        self._clock = clock
        self._scripts = {target_id: list(script) for target_id, script in scripts.items()}
        self._latency_s = latency_s
        self._on_call = on_call
        self.calls: list[Call] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def starts(self, target_id: str | None = None) -> list[datetime]:
        return [c.started for c in self.calls if target_id in (None, c.target_id)]

    async def check_availability(self, target: Target) -> Snapshot:
        call = Call(target.id, self._clock.now())
        self.calls.append(call)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._on_call is not None:
                await self._on_call(target)
            if self._latency_s:
                await self._clock.sleep(self._latency_s)
            script = self._scripts[target.id]
            response = script.pop(0) if len(script) > 1 else script[0]
            if isinstance(response, BaseException):
                raise response
            return Snapshot(
                target.id,
                self._clock.now(),
                {day: tuple(shows) for day, shows in response.items()},
            )
        finally:
            self.in_flight -= 1
            call.ended = self._clock.now()
