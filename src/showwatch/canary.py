"""The canary: a read-only check that the booking flow still matches the adapter (T8.8, M10.2).

It opens a show, chooses the number of seats, reads the seat map, and checks that the "proceed"
button exists, without clicking it. It never clicks a seat, holds nothing, and so stops well
short of payment. If a step fails, the user is told, because a real seat hold would fail the
same way.

Locators healed by Claude are used too: a pass promotes the provisional ones, and a failure
discards the provisional ones for the failing step (T9.4).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from showwatch.adapters.base import LocatorSpec, SiteAdapter
from showwatch.clock import Clock
from showwatch.errors import LocatorNotFound
from showwatch.executor.guards import Step, safe_click, safe_select
from showwatch.models import Event, EventKind
from showwatch.notify.base import Notifier
from showwatch.store import Store

log = logging.getLogger(__name__)

STEP_LOCATORS: dict[Step, tuple[str, ...]] = {
    Step.CHOOSE_QUANTITY: ("choose_quantity", "select_seats"),
    Step.SELECT_SEAT: ("seat",),
    Step.PROCEED_TO_SUMMARY: ("proceed_to_summary",),
}


@dataclass(frozen=True)
class CanaryResult:
    passed: bool
    step: str | None = None  # where it failed
    error: str | None = None


class Canary:
    def __init__(
        self,
        *,
        adapter: SiteAdapter,
        notifier: Notifier,
        clock: Clock,
        open_page: Callable[[], Awaitable[Page]],
        step_timeout_ms: int = 10_000,
        seats: int = 2,
        target_id: str | None = None,
        movie: str | None = None,
        store: Store | None = None,
    ) -> None:
        self._adapter = adapter
        self._notifier = notifier
        self._clock = clock
        self._open_page = open_page
        self._step_timeout_ms = step_timeout_ms
        self._seats = seats
        self._target_id = target_id
        self._movie = movie
        self._store = store
        self._locators: dict[str, LocatorSpec] = dict(adapter.locators())
        self._provisional: list[str] = []

    async def run(self, show_url: str) -> CanaryResult:
        self._locators = dict(self._adapter.locators())
        self._provisional = []
        if self._store is not None:
            for name, override in (await self._store.locator_overrides()).items():
                self._locators[name] = override.spec
                if override.status == "provisional":
                    self._provisional.append(name)
        page = await self._open_page()
        page.set_default_timeout(self._step_timeout_ms)
        step = Step.OPEN_SHOW
        try:
            await page.goto(show_url)
            await self._inspect(page, step)
            step = Step.CHOOSE_QUANTITY
            count = await self._find(page, "choose_quantity", step)
            await safe_select(page, count, str(self._seats), self._adapter, step=step)
            spec = self._locators["select_seats"]
            button = await self._find(page, "select_seats", step)
            await safe_click(page, button, self._adapter, step=step, navigates=spec.navigates)
            await self._inspect(page, step)
            step = Step.SELECT_SEAT
            seat_map = await self._adapter.read_seat_map(page)
            if not seat_map.rows:
                raise LocatorNotFound(step.value)
            step = Step.PROCEED_TO_SUMMARY
            proceed = self._locators["proceed_to_summary"].resolve(page)
            if await proceed.count() == 0:  # present (it may be disabled), never clicked
                raise LocatorNotFound(step.value)
        except (LocatorNotFound, PlaywrightError, RuntimeError) as exc:
            return await self._failed(step, exc)
        finally:
            await page.close()
        log.info("canary passed: open show, choose seats, seat map, proceed button all found")
        if self._store is not None and self._provisional:
            await self._store.confirm_locators(self._provisional)
            log.info("healed locators confirmed: %s", ", ".join(self._provisional))
        return CanaryResult(passed=True)

    async def _find(self, page: Page, name: str, step: Step) -> Locator:
        locator = self._locators[name].resolve(page)
        try:
            await locator.wait_for(state="visible")
        except PlaywrightTimeoutError:
            raise LocatorNotFound(step.value) from None
        return locator

    async def _inspect(self, page: Page, step: Step) -> None:
        kind = await self._adapter.detect_interstitial(page)
        if kind is not None:
            raise RuntimeError(f"the site showed {kind} during {step.value}")

    async def _failed(self, step: Step, exc: Exception) -> CanaryResult:
        where = exc.step if isinstance(exc, LocatorNotFound) else step.value
        log.warning("canary failed at %s: %s", where, exc)
        doubtful = [n for n in self._provisional if n in STEP_LOCATORS.get(Step(where), ())]
        if self._store is not None and doubtful:
            await self._store.discard_locators(doubtful)
            log.warning("healed locators discarded: %s", ", ".join(doubtful))
        await self._notifier.send(
            Event(
                event_id=f"canary:{where}:{self._clock.now().isoformat()}",
                kind=EventKind.CANARY_FAILED,
                at=self._clock.now(),
                target_id=self._target_id,
                movie=self._movie or "ShowWatch canary",
                reason=where,
            )
        )
        return CanaryResult(passed=False, step=where, error=str(exc))
