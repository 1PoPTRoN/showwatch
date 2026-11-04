"""The orchestrator: wires the watcher, executor and housekeeping together (SPEC §3).

Startup:
  1. `Store.recover()` (SPEC §3 invariants). Holds still running get their countdown again,
     under a new event id (docs/DESIGN-NOTES.md P3.4). An opening whose alert may never have
     been sent is alerted again (deduped). An interrupted selection is reported as INTERRUPTED.
  2. IDLE targets start watching, unless all of their dates have passed.

Then one loop (`WatchLoop`), on one clock, does everything:
  * it polls WATCHING targets. A detection in dry-run or assist mode is handed to the executor;
    notify-only targets never touch the browser (T8.1);
  * housekeeping before every step: unpaid holds whose time is up → EXPIRED → (rewatch) →
    WATCHING; leftover EXPIRED or RELEASED targets → rewatch; targets whose dates have all
    passed → PAUSED; and the daily heartbeat.
"""

import logging
import random
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from pathlib import Path

from playwright.async_api import Page

from showwatch.adapters.base import SiteAdapter
from showwatch.clock import Clock
from showwatch.config import Mode, Settings
from showwatch.errors import IllegalTransition
from showwatch.executor.executor import Executor, StepHook
from showwatch.llm.healer import Healer
from showwatch.models import Attempt, Event, EventKind
from showwatch.notify.base import Notifier
from showwatch.notify.heartbeat import heartbeat_event
from showwatch.ratelimit import PollGate
from showwatch.state import State, Trigger
from showwatch.store import Recovery, Store, TransitionResult
from showwatch.watcher.watcher import Watcher, WatchLoop

log = logging.getLogger(__name__)

_HOLDING = (State.HELD, State.AWAITING_PAYMENT)


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        adapter: SiteAdapter,
        notifier: Notifier,
        clock: Clock,
        rng: random.Random,
        open_page: Callable[[], Awaitable[Page]],
        runs_dir: Path,
        step_timeout_ms: int = 10_000,
        on_step: StepHook | None = None,
        healer: Healer | None = None,
        gate: PollGate | None = None,  # shared with an adapter that paces extra requests
    ) -> None:
        self._settings = settings
        self._store = store
        self._notifier = notifier
        self._clock = clock
        self._tz = settings.tz
        self.gate = gate or PollGate(clock)
        self._executor = Executor(
            store=store,
            adapter=adapter,
            notifier=notifier,
            clock=clock,
            gate=self.gate,
            tz=self._tz,
            open_page=open_page,
            runs_dir=runs_dir,
            step_timeout_ms=step_timeout_ms,
            on_step=on_step,
            healer=healer,
        )
        watcher = Watcher(
            store=store,
            adapter=adapter,
            notifier=notifier,
            clock=clock,
            gate=self.gate,
            polling=settings.polling,
            tz=self._tz,
            on_detected=self._on_detected,
        )
        self._loop = WatchLoop(
            watcher,
            store=store,
            clock=clock,
            polling=settings.polling,
            tz=self._tz,
            rng=rng,
            housekeeping=self._housekeeping,
        )
        self._started = False
        self._heartbeat_sent_for: date | None = None

    async def run(self, *, until: datetime | None = None, max_polls: int | None = None) -> int:
        """Start (once), then watch until `until` or `max_polls`. Returns the number of polls."""
        if not self._started:
            await self.start()
        return await self._loop.run(until=until, max_polls=max_polls)

    async def start(self) -> Recovery:
        self._started = True
        recovery = await self._store.recover()
        await self._renotify(recovery)
        today = self._today()
        for record in await self._store.list_targets():
            if record.state is State.IDLE:
                if max(record.target.dates) < today:
                    await self._dates_passed(record.target.id, record.target.movie)
                else:
                    await self._store.fire(record.target.id, Trigger.START)
        return recovery

    # --- detections ----------------------------------------------------------------------------

    async def _on_detected(self, result: TransitionResult) -> None:
        target = (await self._store.get_target(result.target_id)).target
        if target.mode is Mode.NOTIFY_ONLY:
            return  # handled by the watcher; never opens a browser
        outcome = await self._executor.run(result)
        log.info("%s: booking run ended: %s", target.id, outcome.outcome)

    # --- housekeeping --------------------------------------------------------------------------

    async def _housekeeping(self) -> datetime | None:
        now = self._clock.now()
        wake: list[datetime] = []
        today = self._today()
        for record in await self._store.list_targets():
            target_id, state = record.target.id, record.state
            if state in _HOLDING:
                attempt = await self._store.open_attempt(target_id)
                expires = attempt.expires_at if attempt is not None else None
                if expires is None or expires > now:
                    if expires is not None:
                        wake.append(expires)
                    continue
                await self._expire(target_id, record.target.movie, attempt)
            elif state in (State.EXPIRED, State.RELEASED):
                await self._fire(target_id, Trigger.REWATCH, "automatic rewatch")
            elif state in (State.IDLE, State.WATCHING) and max(record.target.dates) < today:
                await self._dates_passed(target_id, record.target.movie)
        heartbeat = await self._heartbeat(now)
        if heartbeat is not None:
            wake.append(heartbeat)
        return min(wake) if wake else None

    async def _expire(self, target_id: str, movie: str, attempt: Attempt | None) -> None:
        if not await self._fire(target_id, Trigger.HOLD_EXPIRED, "the hold ran out unpaid"):
            return
        if attempt is not None:
            await self._notifier.send(
                Event(
                    event_id=f"{attempt.attempt_id}:expired",
                    kind=EventKind.EXPIRED,
                    at=self._clock.now(),
                    target_id=target_id,
                    movie=movie,
                    attempt_id=attempt.attempt_id,
                    show=attempt.held.show if attempt.held else None,
                    seats=attempt.held.seats if attempt.held else (),
                )
            )
        await self._fire(target_id, Trigger.REWATCH, "automatic rewatch")

    async def _dates_passed(self, target_id: str, movie: str) -> None:
        if await self._fire(target_id, Trigger.DATES_PASSED, "every date has passed"):
            await self._notifier.send(
                Event(
                    event_id=f"{target_id}:dates_passed",
                    kind=EventKind.DATES_PASSED,
                    at=self._clock.now(),
                    target_id=target_id,
                    movie=movie,
                )
            )

    async def _heartbeat(self, now: datetime) -> datetime | None:
        """Send today's heartbeat once it is due. Returns the next heartbeat time."""
        at = self._settings.heartbeat_at
        if at is None:
            return None
        local_today = now.astimezone(self._tz).date()
        due = self._settings.local_datetime(local_today, at)
        if now >= due and self._heartbeat_sent_for != local_today:
            self._heartbeat_sent_for = local_today
            await self._notifier.send(await heartbeat_event(self._store, self._clock, tz=self._tz))
        if now < due:
            return due
        return self._settings.local_datetime(date.fromordinal(local_today.toordinal() + 1), at)

    # --- startup ----------------------------------------------------------------------------------

    async def _renotify(self, recovery: Recovery) -> None:
        boot = self._clock.now().isoformat()
        movies = {r.target.id: r.target.movie for r in await self._store.list_targets()}
        recovered_from = {t.attempt_id: t.from_state for t in recovery.transitions if t.attempt_id}
        for attempt in recovery.holds_still_running:
            held = attempt.held
            await self._notifier.send(
                Event(
                    event_id=f"{attempt.attempt_id}:held:recovered:{boot}",
                    kind=EventKind.HELD,
                    at=self._clock.now(),
                    target_id=attempt.target_id,
                    movie=movies.get(attempt.target_id),
                    attempt_id=attempt.attempt_id,
                    show=held.show if held else None,
                    seats=held.seats if held else (),
                    expires_at=attempt.expires_at,
                )
            )
        for attempt in recovery.interrupted:
            was_detected = recovered_from.get(attempt.attempt_id) is State.DETECTED
            suffix = "detected" if was_detected else "interrupted"
            await self._notifier.send(
                Event(
                    # The original id: if that alert already went out, dedup drops this one.
                    event_id=f"{attempt.attempt_id}:{suffix}",
                    kind=EventKind.DETECTED if was_detected else EventKind.INTERRUPTED,
                    at=self._clock.now(),
                    target_id=attempt.target_id,
                    movie=movies.get(attempt.target_id),
                    attempt_id=attempt.attempt_id,
                    changes=attempt.changes,
                )
            )

    # --- helpers ---------------------------------------------------------------------------------

    async def _fire(self, target_id: str, trigger: Trigger, reason: str) -> bool:
        try:
            await self._store.fire(target_id, trigger, reason=reason)
        except IllegalTransition as exc:  # someone else (a CLI command) got there first
            log.info("%s: %s skipped: %s", target_id, trigger.value, exc)
            return False
        return True

    def _today(self) -> date:
        return self._clock.now().astimezone(self._tz).date()
