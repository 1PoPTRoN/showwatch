"""The executor: from a detected opening to seats held at the order summary, and no further (C1).

Assist mode: BEGIN_SELECT, then for the best-ranked shows in turn: open the show, choose the number
of seats, read the seat map, pick `best_block`, click those seats, and proceed to the order
summary. Then SEATS_HELD, an alert with the countdown, and SUMMARY_REACHED. The browser is left on
the order summary, and the user pays. If someone else takes a seat first, the map is read again
and the next best block is tried, up to MAX_SEAT_TRIES tries in all.

Dry-run mode: the same up to the seat map. Then the chosen seats are outlined in a screenshot,
and nothing is clicked.

Every click goes through `guards.safe_click`. The whole run holds `PollGate.exclusive()`, so no
poll happens meanwhile (docs/DESIGN-NOTES.md P4.6). After every page load the adapter checks for
interstitials:
  * a CAPTCHA, queue or block page → BLOCKED → PAUSED. Nothing more is done and the page is left
    exactly as it is (C3).
  * a login wall → FAILED (`needs_login`).
A missing element raises LocatorNotFound(step): screenshot, then FAILED (`locator_not_found`).
With a healer configured (`llm.healer: true`, off by default), a missing role-based element first
gets one heal per step per attempt. An accepted proposal is used at once and stored as
provisional (Phase 9).
Screenshots go to `<runs_dir>/<attempt_id>/NN-<step>.png`.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from showwatch.adapters.base import LocatorSpec, SiteAdapter
from showwatch.clock import Clock
from showwatch.config import Mode, Target
from showwatch.errors import LlmError, LocatorNotFound, PaymentBoundaryError
from showwatch.executor.guards import Step, safe_click, safe_select
from showwatch.llm.healer import Healer
from showwatch.matcher import best_block, rank_shows
from showwatch.models import Attempt, Event, EventKind, HeldSeats, SeatMap, SeatStatus, Show
from showwatch.notify.base import Notifier
from showwatch.ratelimit import PollGate
from showwatch.state import State, Trigger
from showwatch.store import Store, TransitionResult

log = logging.getLogger(__name__)

__all__ = ["ExecutionResult", "Executor", "Outcome", "Step"]

StepHook = Callable[[Step], Awaitable[None]]
_HIGHLIGHT = "el => { el.style.outline = '4px solid #d6007a'; el.style.outlineOffset = '2px'; }"


class Outcome(StrEnum):
    HELD = "held"
    DRY_RUN = "dry_run"
    NO_MATCH = "no_match"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class ExecutionResult:
    outcome: Outcome
    attempt_id: str
    held: HeldSeats | None = None
    error: Exception | None = None
    page: Page | None = None  # left open for HELD, BLOCKED and FAILED
    screenshots: list[Path] = field(default_factory=list)


class _Interstitial(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _SeatRaceLost(Exception):
    pass


class Executor:
    MAX_SEAT_TRIES = 3
    MAX_SHOWS = 3  # look at no more than the 3 best shows: polite, and fast enough

    def __init__(
        self,
        *,
        store: Store,
        adapter: SiteAdapter,
        notifier: Notifier,
        clock: Clock,
        gate: PollGate,
        tz: ZoneInfo,
        open_page: Callable[[], Awaitable[Page]],
        runs_dir: Path,
        step_timeout_ms: int = 10_000,
        on_step: StepHook | None = None,
        healer: Healer | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._notifier = notifier
        self._clock = clock
        self._gate = gate
        self._tz = tz
        self._open_page = open_page
        self._runs_dir = runs_dir
        self._step_timeout_ms = step_timeout_ms
        self._on_step = on_step
        self._healer = healer

    async def run(self, detection: TransitionResult) -> ExecutionResult:
        """Act on a CHANGE_DETECTED transition (the watcher's `on_detected` hook)."""
        if detection.attempt_id is None:
            raise ValueError("a detection always has an attempt")
        target = (await self._store.get_target(detection.target_id)).target
        attempt = await self._store.get_attempt(detection.attempt_id)
        await self._store.fire(target.id, Trigger.BEGIN_SELECT)
        run = _Run(self, target, attempt)
        async with self._gate.exclusive():
            return await run.execute()


class _Run:
    """One attempt's browser session."""

    def __init__(self, executor: Executor, target: Target, attempt: Attempt) -> None:
        self.x = executor
        self.target = target
        self.attempt = attempt
        self.folder = executor._runs_dir / attempt.attempt_id
        self.page: Page | None = None
        self.shots: list[Path] = []
        self.step: Step = Step.OPEN_SHOW
        self.locators: dict[str, LocatorSpec] = dict(executor._adapter.locators())
        self.healed: set[Step] = set()  # at most one heal per step per attempt (T9.6)

    def result(self, outcome: Outcome, **fields: object) -> ExecutionResult:
        return ExecutionResult(
            outcome,
            self.attempt.attempt_id,
            screenshots=self.shots,
            **fields,  # type: ignore[arg-type]
        )

    # --- the run --------------------------------------------------------------------------------

    async def execute(self) -> ExecutionResult:
        overrides = await self.x._store.locator_overrides()
        self.locators.update({name: o.spec for name, o in overrides.items()})
        offered = [s for change in self.attempt.changes for s in change.shows]
        shows = rank_shows(offered, self.target, tz=self.x._tz)[: Executor.MAX_SHOWS]
        if not shows:
            return await self.no_match("no listed show matches the rules")
        page = await self.x._open_page()
        page.set_default_timeout(self.x._step_timeout_ms)
        self.page = page
        try:
            for show in shows:
                outcome = await self.try_show(page, show)
                if outcome is not None:
                    return outcome
            return await self.no_match("no seats match the rules")
        except _Interstitial as exc:
            return await self.blocked(exc.kind)
        except _SeatRaceLost as exc:
            return await self.failed("seat_race_exhausted", exc)
        except LocatorNotFound as exc:
            await self.screenshot(f"{exc.step}-not-found")
            return await self.failed("locator_not_found", exc, detail=exc.step)
        except PaymentBoundaryError as exc:  # never expected: the steps stop before payment
            log.error("%s: payment boundary hit: %s", self.target.id, exc)
            return await self.failed("payment_boundary", exc)
        except PlaywrightError as exc:
            await self.screenshot("error")
            log.error("%s: browser error during %s: %s", self.target.id, self.step, exc)
            return await self.failed("browser_error", exc, detail=self.step.value)

    async def try_show(self, page: Page, show: Show) -> ExecutionResult | None:
        for attempt_no in range(1, Executor.MAX_SEAT_TRIES + 1):
            await self.open_show(page, show)
            await self.choose_quantity(page)
            seat_map = await self.x._adapter.read_seat_map(page)
            block = best_block(
                seat_map, self.target.seats, self.target.preferred_rows, self.target.avoid_rows
            )
            if block is None:
                log.info("%s: no matching block at %s", self.target.id, show.show_id)
                return None
            cols = range(block.start_col, block.start_col + block.n)
            labels = tuple([await self.seat_label(page, block.row, col) for col in cols])
            held = HeldSeats(show, labels)
            if self.target.mode is Mode.DRY_RUN:
                return await self.dry_run(page, block.row, cols, held)
            if await self.select_block(page, block.row, cols):
                return await self.hold(page, held)
            log.info("%s: lost the race for %s (try %d)", self.target.id, labels, attempt_no)
        raise _SeatRaceLost(f"lost {Executor.MAX_SEAT_TRIES} seat races")

    # --- steps ----------------------------------------------------------------------------------

    async def begin(self, step: Step) -> None:
        self.step = step
        if self.x._on_step is not None:
            await self.x._on_step(step)

    async def inspect_page(self, page: Page) -> None:
        """Stop at once on an interstitial, or if the payment page somehow appeared."""
        kind = await self.x._adapter.detect_interstitial(page)
        if kind == "login":
            raise _Interstitial("login")
        if kind is not None:
            raise _Interstitial(kind)
        if await self.x._adapter.is_payment_page(page):
            raise PaymentBoundaryError("landed on the payment page")

    async def find(self, page: Page, name: str, step: Step, **values: object) -> Locator:
        spec = self.locators[name]
        locator = spec.resolve(page, **values)
        try:
            await locator.wait_for(state="visible")
        except PlaywrightTimeoutError:
            pass
        else:
            return locator
        healer = self.x._healer
        if healer is None or values or step in self.healed:  # seats (parameterised) are not healed
            raise LocatorNotFound(step.value)
        self.healed.add(step)
        try:
            replacement = await healer.heal(page, step=step, name=name, spec=spec)
        except LlmError as exc:
            log.warning("%s: could not heal %s: %s", self.target.id, name, exc)
            raise LocatorNotFound(step.value) from None
        await self.x._store.save_provisional_locator(name, replacement)
        self.locators[name] = replacement
        log.warning(
            "%s: %s healed to %s %r (provisional until a canary passes)",
            self.target.id,
            name,
            replacement.role,
            replacement.name,
        )
        return replacement.resolve(page)

    async def open_show(self, page: Page, show: Show) -> None:
        await self.begin(Step.OPEN_SHOW)
        await page.goto(show.url)
        await self.inspect_page(page)
        await self.screenshot(Step.OPEN_SHOW)

    async def choose_quantity(self, page: Page) -> None:
        await self.begin(Step.CHOOSE_QUANTITY)
        count = await self.find(page, "choose_quantity", Step.CHOOSE_QUANTITY)
        await safe_select(
            page, count, str(self.target.seats), self.x._adapter, step=Step.CHOOSE_QUANTITY
        )
        button = await self.find(page, "select_seats", Step.CHOOSE_QUANTITY)
        spec = self.locators["select_seats"]  # after find(): it may have been healed
        await safe_click(
            page, button, self.x._adapter, step=Step.CHOOSE_QUANTITY, navigates=spec.navigates
        )
        await self.inspect_page(page)
        await self.screenshot(Step.CHOOSE_QUANTITY)

    async def seat_label(self, page: Page, row: str, col: int) -> str:
        seat = await self.find(page, "seat", Step.SELECT_SEAT, row=row, col=col)
        return (await seat.get_attribute("aria-label")) or (await seat.inner_text()).strip()

    async def select_block(self, page: Page, row: str, cols: range) -> bool:
        """Click each seat of the block. False if one was taken by someone else first."""
        spec = self.locators["seat"]
        for col in cols:
            await self.begin(Step.SELECT_SEAT)
            seat = await self.find(page, "seat", Step.SELECT_SEAT, row=row, col=col)
            await safe_click(
                page, seat, self.x._adapter, step=Step.SELECT_SEAT, navigates=spec.navigates
            )
            await self.inspect_page(page)
            await self.screenshot(Step.SELECT_SEAT)
            if (
                _status(await self.x._adapter.read_seat_map(page), row, col)
                is not SeatStatus.AVAILABLE
            ):
                return False
        return True

    async def hold(self, page: Page, held: HeldSeats) -> ExecutionResult:
        await self.begin(Step.PROCEED_TO_SUMMARY)
        proceed = await self.find(page, "proceed_to_summary", Step.PROCEED_TO_SUMMARY)
        spec = self.locators["proceed_to_summary"]  # after find(): it may have been healed
        await safe_click(
            page, proceed, self.x._adapter, step=Step.PROCEED_TO_SUMMARY, navigates=spec.navigates
        )
        await self.inspect_page(page)
        await self.screenshot(Step.PROCEED_TO_SUMMARY)
        expires = await self.x._adapter.read_hold_expiry(page)
        store, target_id = self.x._store, self.target.id
        await store.fire(target_id, Trigger.SEATS_HELD, expires_at=expires, held=held)
        await self.notify(EventKind.HELD, held=held, expires_at=expires)
        await store.fire(target_id, Trigger.SUMMARY_REACHED, expires_at=expires)
        log.info(
            "%s: holding %s at %s; waiting for the user to pay",
            target_id,
            held.seats,
            held.show.venue,
        )
        return self.result(Outcome.HELD, held=held, page=page)

    async def dry_run(self, page: Page, row: str, cols: range, held: HeldSeats) -> ExecutionResult:
        for col in cols:  # outline only: no click, no request
            seat = await self.find(page, "seat", Step.SELECT_SEAT, row=row, col=col)
            await seat.evaluate(_HIGHLIGHT)
        await self.screenshot("dry_run")
        await self.x._store.fire(self.target.id, Trigger.DRY_RUN_DONE)
        await self.notify(EventKind.DRY_RUN, held=held)
        await page.close()
        return self.result(Outcome.DRY_RUN, held=held)

    # --- endings --------------------------------------------------------------------------------

    async def no_match(self, why: str) -> ExecutionResult:
        await self.x._store.fire(self.target.id, Trigger.NO_MATCHING_SEATS, reason=why)
        await self.notify(EventKind.NO_MATCH, reason=why)
        if self.page is not None:
            await self.page.close()
        return self.result(Outcome.NO_MATCH)

    async def blocked(self, kind: str) -> ExecutionResult:
        if kind == "login":
            return await self.failed("needs_login", None)
        await self.screenshot("blocked")  # local only: no request to the site
        await self.x._store.fire(self.target.id, Trigger.BLOCKED, reason=kind)
        await self.notify(EventKind.BLOCKED, reason=kind)
        log.warning(
            "%s: %s during %s; paused, browser left as it is", self.target.id, kind, self.step
        )
        return self.result(Outcome.BLOCKED, page=self.page)

    async def failed(
        self, reason: str, error: Exception | None, detail: str | None = None
    ) -> ExecutionResult:
        state = (await self.x._store.get_target(self.target.id)).state
        if state is State.SELECTING:
            audit_reason = reason if detail is None else f"{reason}:{detail}"
            await self.x._store.fire(
                self.target.id, Trigger.ERROR, reason=reason if detail is None else audit_reason
            )
            await self.notify(EventKind.FAILED, reason=reason)
        else:
            log.error(
                "%s: %s after leaving selection (%s); left as it is", self.target.id, reason, state
            )
        return self.result(Outcome.FAILED, error=error, page=self.page)

    async def notify(
        self,
        kind: EventKind,
        *,
        held: HeldSeats | None = None,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> None:
        await self.x._notifier.send(
            Event(
                event_id=f"{self.attempt.attempt_id}:{kind.value}",
                kind=kind,
                at=self.x._clock.now(),
                target_id=self.target.id,
                movie=self.target.movie,
                attempt_id=self.attempt.attempt_id,
                changes=self.attempt.changes,
                reason=reason,
                show=held.show if held else None,
                seats=held.seats if held else (),
                expires_at=expires_at,
            )
        )

    async def screenshot(self, label: str) -> None:
        if self.page is None or self.page.is_closed():
            return
        await asyncio.to_thread(self.folder.mkdir, parents=True, exist_ok=True)
        path = self.folder / f"{len(self.shots) + 1:02d}-{label}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
        except PlaywrightError as exc:
            log.warning("%s: could not save screenshot %s: %s", self.target.id, path.name, exc)
            return
        self.shots.append(path)


def _status(seat_map: SeatMap, row: str, col: int) -> SeatStatus | None:
    for label, cells in seat_map.rows:
        if label == row:
            return cells[col] if 0 <= col < len(cells) else None
    return None
