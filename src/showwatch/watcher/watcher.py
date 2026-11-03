"""The watcher: one poll of one target, and the single loop that polls every WATCHING target.

`Watcher.poll(target_id)`:
  1. Only a WATCHING target is polled.
  2. The check runs inside the global PollGate (20 s spacing, one request in flight; C4).
  3. Then, by result:
       * a snapshot: diff its relevant part against everything seen so far, then save the poll and
         any detection in one transaction (`Store.record_poll`), and alert the user;
       * RateLimited: tell the loop, which backs off every target;
       * Blocked: PAUSE the target and tell the user once. Nothing is evaded (C3);
       * anything else: a failed poll. This target backs off, and the user is told at the 5th
         failure in a row.
  4. After a detection, a notify-only target fires `notified` and goes back to WATCHING. A dry-run
     or assist target is handed to `on_detected` (the executor, Phases 7-8).

`WatchLoop.run()` is a single task. It picks the WATCHING target that is due soonest, sleeps on the
injected Clock until then, polls it and schedules the next poll (`scheduler.next_delay`). As one
task, its polls can never overlap; the gate also guards against any other caller.
"""

import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from showwatch.adapters.base import AvailabilityChecker
from showwatch.clock import Clock
from showwatch.config import Mode, Polling, Target
from showwatch.errors import Blocked, IllegalTransition, RateLimited, TransientError
from showwatch.models import Change, Event, EventKind, Snapshot
from showwatch.notify.base import Notifier
from showwatch.ratelimit import Backoff, PollGate
from showwatch.state import State, Trigger
from showwatch.store import Store, TransitionResult
from showwatch.watcher.diff import diff, disappeared, merge_seen, relevant_view
from showwatch.watcher.scheduler import next_delay

log = logging.getLogger(__name__)

FAILURES_BEFORE_ALERT = 5

DetectedHook = Callable[[TransitionResult], Awaitable[None]]


class PollOutcome(StrEnum):
    BASELINE = "baseline"  # the first successful poll: recorded, nothing detected
    UNCHANGED = "unchanged"
    CHANGED = "changed"  # an attempt was opened
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"  # the target is now PAUSED
    FAILED = "failed"
    SKIPPED = "skipped"  # the target was not WATCHING


class Watcher:
    def __init__(
        self,
        *,
        store: Store,
        adapter: AvailabilityChecker,
        notifier: Notifier,
        clock: Clock,
        gate: PollGate,
        polling: Polling,
        tz: ZoneInfo,
        on_detected: DetectedHook | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._notifier = notifier
        self._clock = clock
        self._gate = gate
        self._polling = polling
        self._tz = tz
        self._on_detected = on_detected
        self._failures: dict[str, int] = {}
        self._failing_since: dict[str, datetime] = {}

    async def poll(self, target_id: str) -> PollOutcome:
        record = await self._store.get_target(target_id)
        if record.state is not State.WATCHING:
            return PollOutcome.SKIPPED
        try:
            async with self._gate.poll():
                snapshot = await self._adapter.check_availability(record.target)
            if snapshot.target_id != target_id:
                raise TransientError(f"adapter answered for {snapshot.target_id!r}")
        except RateLimited:
            log.warning("%s: rate limited (429); backing off every target", target_id)
            await self._store.record_poll_failure(target_id)
            return PollOutcome.RATE_LIMITED
        except Blocked as exc:
            await self._store.record_poll_failure(target_id)
            await self._blocked(target_id, exc.reason)
            return PollOutcome.BLOCKED
        except Exception as exc:  # noqa: BLE001 (a long-running watcher must survive any failure)
            await self._store.record_poll_failure(target_id)
            await self._failed(target_id, exc)
            return PollOutcome.FAILED
        self._failures.pop(target_id, None)
        self._failing_since.pop(target_id, None)
        return await self._observed(record.target, snapshot)

    # --- outcomes ------------------------------------------------------------------------------

    async def _observed(self, target: Target, snapshot: Snapshot) -> PollOutcome:
        state = await self._store.watch_state(target.id)
        view = relevant_view(snapshot, target, tz=self._tz)
        if state.snapshot is not None:
            gone = disappeared(relevant_view(state.snapshot, target, tz=self._tz), view)
            if gone:
                log.info(
                    "%s: %d show(s) no longer listed: %s",
                    target.id,
                    len(gone),
                    ", ".join(s.show_id for s in gone),
                )
        changes = diff(state.seen, view)
        result = await self._store.record_poll(
            target.id, snapshot=snapshot, seen=merge_seen(state.seen, view), changes=changes
        )
        if state.seen is None:
            if view.open_dates:
                already = diff(Snapshot(target.id, view.taken_at, {}), view)
                await self._notify(
                    Event(
                        f"{target.id}:already_open",
                        EventKind.ALREADY_OPEN,
                        target.id,
                        self._clock.now(),
                        changes=tuple(already),
                    )
                )
            return PollOutcome.BASELINE
        if result is None:
            if changes:
                log.info("%s: new shows found, but it stopped watching; kept for later", target.id)
            return PollOutcome.UNCHANGED
        await self._detected(target, result, changes)
        return PollOutcome.CHANGED

    async def _detected(
        self, target: Target, result: TransitionResult, changes: list[Change]
    ) -> None:
        await self._notify(
            Event(
                f"{result.attempt_id}:detected",
                EventKind.DETECTED,
                target.id,
                result.at,
                attempt_id=result.attempt_id,
                changes=tuple(changes),
            )
        )
        if target.mode is Mode.NOTIFY_ONLY:
            try:
                await self._store.fire(target.id, Trigger.NOTIFIED)
            except IllegalTransition:
                log.warning("%s: alert sent, but the target had already moved on", target.id)
        elif self._on_detected is not None:
            try:
                await self._on_detected(result)
            except Exception:
                log.exception("%s: seat selection hook failed", target.id)
        else:
            log.warning(
                "%s: detected, but no seat selection is wired up (%s)", target.id, target.mode
            )

    async def _blocked(self, target_id: str, reason: str) -> None:
        try:
            result = await self._store.fire(target_id, Trigger.BLOCKED, reason=reason)
        except IllegalTransition:
            log.warning("%s: blocked (%s), but it was no longer watching", target_id, reason)
            return
        log.warning("%s: blocked (%s); paused until you resume it", target_id, reason)
        await self._notify(
            Event(
                f"{target_id}:blocked:{result.at.isoformat()}",
                EventKind.BLOCKED,
                target_id,
                result.at,
                reason=reason,
            )
        )

    async def _failed(self, target_id: str, exc: Exception) -> None:
        now = self._clock.now()
        count = self._failures.get(target_id, 0) + 1
        self._failures[target_id] = count
        since = self._failing_since.setdefault(target_id, now)
        if isinstance(exc, TransientError):
            log.warning("%s: check failed (%d in a row): %s", target_id, count, exc)
        else:
            log.error("%s: unexpected error (%d in a row)", target_id, count, exc_info=exc)
        if count == FAILURES_BEFORE_ALERT:
            await self._notify(
                Event(
                    f"{target_id}:poll_failing:{since.isoformat()}",
                    EventKind.POLL_FAILING,
                    target_id,
                    now,
                    reason=type(exc).__name__,
                )
            )

    async def _notify(self, event: Event) -> None:
        try:
            await self._notifier.send(event)
        except Exception:
            log.exception("could not send the %s alert for %s", event.kind, event.target_id)


class WatchLoop:
    """Polls every WATCHING target, one at a time, for as long as it runs."""

    def __init__(
        self,
        watcher: Watcher,
        *,
        store: Store,
        clock: Clock,
        polling: Polling,
        tz: ZoneInfo,
        rng: random.Random,
        idle_s: float = 60.0,
    ) -> None:
        self._watcher = watcher
        self._store = store
        self._clock = clock
        self._polling = polling
        self._tz = tz
        self._rng = rng
        self._idle_s = idle_s
        self._due: dict[str, datetime] = {}
        self._global = Backoff()  # 429: the site is limiting us, so every target slows down
        self._own: dict[str, Backoff] = {}  # failures: only the failing target slows down

    async def run(self, *, until: datetime | None = None, max_polls: int | None = None) -> int:
        """Poll until `until` (virtual time) or `max_polls` polls. Returns the number of polls."""
        polls = 0
        while max_polls is None or polls < max_polls:
            now = self._clock.now()
            watching = {
                r.target.id for r in await self._store.list_targets() if r.state is State.WATCHING
            }
            for target_id in set(self._due) - watching:
                del self._due[target_id]
            for target_id in watching - set(self._due):
                self._due[target_id] = now  # newly watching: poll as soon as the gate allows
            if not self._due:
                if until is not None and now >= until:
                    break
                remaining = None if until is None else (until - now).total_seconds()
                await self._clock.sleep(
                    self._idle_s if remaining is None else min(self._idle_s, remaining)
                )
                continue
            target_id = min(self._due, key=lambda t: (self._due[t], t))
            due = self._due[target_id]
            if until is not None and due > until:
                break
            wait = (due - now).total_seconds()
            if wait > 0:
                await self._clock.sleep(wait)
            outcome = await self._watcher.poll(target_id)
            polls += 1
            self._schedule(target_id, outcome)
        return polls

    def _schedule(self, target_id: str, outcome: PollOutcome) -> None:
        own = self._own.setdefault(target_id, Backoff())
        if outcome in (PollOutcome.BLOCKED, PollOutcome.SKIPPED):
            self._due.pop(target_id, None)
            return
        if outcome is PollOutcome.RATE_LIMITED:
            self._global.failure()
        elif outcome is PollOutcome.FAILED:
            own.failure()
        else:
            self._global.success()
            own.success()
        now = self._clock.now()
        self._due[target_id] = now + self._delay(target_id, now)
        if outcome is PollOutcome.RATE_LIMITED:
            for other in self._due:
                if other != target_id:
                    self._due[other] = max(self._due[other], now + self._delay(other, now))

    def _delay(self, target_id: str, now: datetime) -> timedelta:
        own = self._own.setdefault(target_id, Backoff())
        multiplier = max(self._global.multiplier, own.multiplier)
        seconds = next_delay(now, self._polling, self._tz, self._rng, multiplier=multiplier)
        return timedelta(seconds=seconds)
