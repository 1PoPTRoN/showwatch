"""Phase 8: the orchestrator end to end (tests T8.1-T8.5, T8.7, T8.8).

A real headless browser and FakeShow, on the virtual clock: half an hour of watching takes
seconds. An instrumented adapter flips booking open right after the baseline poll(s), so
detection latency can be measured exactly.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.async_api import Page
from typer.testing import CliRunner

from showwatch.adapters.base import Interstitial, LocatorSet
from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.canary import Canary
from showwatch.cli import app
from showwatch.config import Settings, Target
from showwatch.executor.guards import Step
from showwatch.models import EventKind, SeatMap, Snapshot
from showwatch.orchestrator import Orchestrator
from showwatch.state import State
from showwatch.store import Store
from tests.support.browser import BrowserSession
from tests.support.clock import FakeClock
from tests.support.fakesite import FakeSite
from tests.support.notify import RecordingNotifier
from tests.support.targets import make_target

IST = ZoneInfo("Asia/Kolkata")
SHOW_A = {
    "show_id": "fri-a-19",
    "venue": "Cinema A",
    "date": "2026-12-18",
    "time": "19:00",
    "format": "IMAX 2D",
    "min_price": 450,
}
SHOW_B = {
    "show_id": "fri-b-20",
    "venue": "Cinema B",
    "date": "2026-12-18",
    "time": "20:00",
    "format": "IMAX 2D",
    "min_price": 450,
}
BASE_S = 300


class FlippingAdapter:
    """FakeSiteAdapter that runs `flip` once, right after the `after`-th availability check."""

    def __init__(
        self, inner: FakeSiteAdapter, clock: FakeClock, flip: Callable[[], None], after: int = 1
    ) -> None:
        self.inner, self.clock, self.flip, self.after = inner, clock, flip, after
        self.calls: list[tuple[str, datetime]] = []
        self.flipped_at: datetime | None = None

    async def check_availability(self, target: Target) -> Snapshot:
        self.calls.append((target.id, self.clock.now()))
        snapshot = await self.inner.check_availability(target)
        if len(self.calls) == self.after:
            self.flip()
            self.flipped_at = self.clock.now()
        return snapshot

    def locators(self) -> LocatorSet:
        return self.inner.locators()

    def forbidden(self) -> LocatorSet:
        return self.inner.forbidden()

    async def detect_interstitial(self, page: Page) -> Interstitial | None:
        return await self.inner.detect_interstitial(page)

    async def is_payment_page(self, page: Page) -> bool:
        return await self.inner.is_payment_page(page)

    async def read_seat_map(self, page: Page) -> SeatMap:
        return await self.inner.read_seat_map(page)

    async def read_hold_expiry(self, page: Page) -> datetime | None:
        return await self.inner.read_hold_expiry(page)


def settings_for(*targets: Target, heartbeat_at: str | None = None) -> Settings:
    return Settings.model_validate(
        {
            "version": 1,
            "notifier": "console",
            "polling": {"base_interval_s": BASE_S, "hot_interval_s": 60, "jitter_pct": 0},
            "heartbeat_at": heartbeat_at,
            "targets": [t.model_dump(mode="json") for t in targets],
        }
    )


class Harness:
    def __init__(
        self,
        store: Store,
        fakesite: FakeSite,
        browser: BrowserSession,
        clock: FakeClock,
        client: httpx.AsyncClient,
        runs: Path,
    ) -> None:
        self.store, self.site, self.browser, self.clock = store, fakesite, browser, clock
        self.client, self.runs = client, runs
        self.notifier = RecordingNotifier()
        self.pages_opened = 0

    async def open_page(self) -> Page:
        self.pages_opened += 1
        return await self.browser.context.new_page()

    async def add(self, *targets: Target) -> None:
        for target in targets:
            await self.store.add_target(target)

    def adapter(self, after: int = 1) -> FlippingAdapter:
        inner = FakeSiteAdapter(self.site.base_url, client=self.client, clock=self.clock)
        return FlippingAdapter(inner, self.clock, lambda: self.site.set(booking_open=True), after)

    def orchestrator(
        self,
        settings: Settings,
        adapter: FlippingAdapter,
        *,
        store: Store | None = None,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
    ) -> Orchestrator:
        return Orchestrator(
            settings=settings,
            store=store or self.store,
            adapter=adapter,
            notifier=self.notifier,
            clock=self.clock,
            rng=random.Random(8),
            open_page=self.open_page,
            runs_dir=self.runs,
            step_timeout_ms=5_000,
            on_step=on_step,
        )

    def later(self, minutes: float) -> datetime:
        return self.clock.now() + timedelta(minutes=minutes)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


@pytest.fixture
def harness(
    store: Store,
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> Harness:
    fakesite.set(booking_open=False, shows=[SHOW_A, SHOW_B])
    return Harness(store, fakesite, browser, fake_clock, client, tmp_path / "runs")


# --- T8.1 ------------------------------------------------------------------------------------


async def test_t8_1_notify_only_alerts_once_and_never_opens_a_browser(harness: Harness) -> None:
    target = make_target(id="t1", mode="notify-only")
    await harness.add(target)
    orchestrator = harness.orchestrator(settings_for(target), harness.adapter())

    await orchestrator.run(until=harness.later(30))

    assert harness.notifier.kinds() == [EventKind.DETECTED]
    assert harness.pages_opened == 0  # no browser at all
    assert (await harness.store.get_target("t1")).state is State.WATCHING
    (attempt,) = await harness.store.attempts("t1")
    assert attempt.outcome == "notified"


# --- T8.2 / T8.3 ------------------------------------------------------------------------------


async def test_t8_2_assist_goes_all_the_way_to_awaiting_payment(harness: Harness) -> None:
    target = make_target(id="t1", mode="assist")
    await harness.add(target)
    adapter = harness.adapter()
    orchestrator = harness.orchestrator(settings_for(target), adapter)

    await orchestrator.run(until=harness.later(8))  # the hold (600 s) is still running

    assert (await harness.store.get_target("t1")).state is State.AWAITING_PAYMENT
    assert harness.notifier.kinds() == [EventKind.DETECTED, EventKind.HELD]
    held = harness.notifier.events[1]
    assert held.expires_at is not None
    assert held.seats
    (attempt,) = await harness.store.attempts("t1")
    assert adapter.flipped_at is not None
    assert attempt.opened_at - adapter.flipped_at <= timedelta(
        seconds=BASE_S
    )  # within one interval


async def test_t8_3_an_unpaid_hold_expires_and_watching_resumes(harness: Harness) -> None:
    harness.site.set(hold_seconds=45)
    target = make_target(id="t1", mode="assist")
    await harness.add(target)
    orchestrator = harness.orchestrator(settings_for(target), harness.adapter())

    await orchestrator.run(until=harness.later(30))

    assert harness.notifier.kinds() == [EventKind.DETECTED, EventKind.HELD, EventKind.EXPIRED]
    assert (await harness.store.get_target("t1")).state is State.WATCHING
    (attempt,) = await harness.store.attempts("t1")  # never re-holds the same shows
    assert attempt.outcome == "expired"
    expired = harness.notifier.events[2]
    assert attempt.expires_at is not None
    assert expired.at == attempt.expires_at  # noticed at the exact second, not a poll later


# --- T8.4 ------------------------------------------------------------------------------------


async def test_t8_4_killed_mid_selection_recovers_without_a_duplicate_attempt(
    harness: Harness, tmp_path: Path
) -> None:
    target = make_target(id="t1", mode="assist")
    await harness.add(target)

    async def die(step: Step) -> None:
        if step is Step.CHOOSE_QUANTITY:
            raise asyncio.CancelledError  # the process is killed right here

    first = harness.orchestrator(settings_for(target), harness.adapter(), on_step=die)
    with pytest.raises(asyncio.CancelledError):
        await first.run(until=harness.later(30))
    assert (await harness.store.get_target("t1")).state is State.SELECTING
    await harness.store.close()

    async with await Store.open(tmp_path / "showwatch.db", clock=harness.clock) as reopened:
        second = harness.orchestrator(
            settings_for(target), harness.adapter(after=10**6), store=reopened
        )
        await second.run(until=harness.later(30))

        assert (await reopened.get_target("t1")).state is State.WATCHING
        assert len(await reopened.attempts("t1")) == 1  # no duplicate attempt
        assert EventKind.INTERRUPTED in harness.notifier.kinds()


# --- T8.5 ------------------------------------------------------------------------------------


async def test_t8_5_two_targets_at_two_venues_never_overlap(harness: Harness) -> None:
    a = make_target(id="t-a", mode="assist", venues=["Cinema A"])
    b = make_target(id="t-b", mode="assist", venues=["Cinema B"])
    await harness.add(a, b)
    orchestrator = harness.orchestrator(settings_for(a, b), harness.adapter(after=2))

    active, peak = 0, 0
    sessions: dict[str, int] = {"poll": 0, "exclusive": 0}

    def tracked(kind: str, open_gate: Callable[[], Any]) -> Callable[[], Any]:
        @asynccontextmanager
        async def wrapper() -> AsyncIterator[None]:
            nonlocal active, peak
            async with open_gate():
                active += 1
                sessions[kind] += 1
                peak = max(peak, active)
                try:
                    yield
                finally:
                    active -= 1

        return wrapper

    gate = orchestrator.gate
    gate.poll = tracked("poll", gate.poll)  # type: ignore[method-assign]
    gate.exclusive = tracked("exclusive", gate.exclusive)  # type: ignore[method-assign]

    await orchestrator.run(until=harness.later(14))

    for target_id in ("t-a", "t-b"):
        assert (await harness.store.get_target(target_id)).state is State.AWAITING_PAYMENT
    venues = {e.show.venue for e in harness.notifier.events if e.kind is EventKind.HELD and e.show}
    assert venues == {"Cinema A", "Cinema B"}
    assert peak == 1  # polls and booking runs never overlapped
    assert sessions["exclusive"] == 2  # both booking runs held the same gate the polls use
    assert sessions["poll"] >= 4


# --- T8.7 ------------------------------------------------------------------------------------


async def test_t8_7_done_books_it_and_polling_stops(
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    fakesite.set(booking_open=False, shows=[SHOW_A])
    home = tmp_path / "home"
    async with await Store.open(home / "showwatch.db", clock=fake_clock) as store:
        harness = Harness(store, fakesite, browser, fake_clock, client, tmp_path / "runs")
        target = make_target(id="t1", mode="assist")
        await harness.add(target)
        adapter = harness.adapter()
        orchestrator = harness.orchestrator(settings_for(target), adapter)
        await orchestrator.run(until=harness.later(8))
        (attempt,) = await store.attempts("t1")

        # The user paid in the browser and tells ShowWatch, from another process.
        result = await asyncio.to_thread(
            CliRunner().invoke, app, ["done", attempt.attempt_id], env={"SHOWWATCH_HOME": str(home)}
        )
        assert result.exit_code == 0, result.output
        assert "BOOKED" in result.output

        polls_before = len(adapter.calls)
        await orchestrator.run(until=harness.later(60))
        assert (await store.get_target("t1")).state is State.BOOKED
        assert len(adapter.calls) == polls_before  # nothing more for a booked target


# --- T8.8 ------------------------------------------------------------------------------------


async def test_t8_8_canary_passes_normally_and_fails_loudly_when_the_site_changes(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW_A])
    notifier = RecordingNotifier()
    canary = Canary(
        adapter=FakeSiteAdapter(fakesite.base_url, client=client, clock=fake_clock),
        notifier=notifier,
        clock=fake_clock,
        open_page=browser.context.new_page,
        step_timeout_ms=1_500,
    )
    show_url = fakesite.url("/show/fri-a-19")
    fakesite.reset_log()

    ok = await canary.run(show_url)
    assert ok.passed, ok.error
    assert notifier.events == []
    assert not [e for e in fakesite.log() if e["method"] == "POST"]  # read-only: no seat clicks

    fakesite.set(rename_locator={"proceed_to_summary": "Book now"})
    broken = await canary.run(show_url)
    assert not broken.passed
    assert broken.step == "proceed_to_summary"
    (event,) = notifier.events
    assert event.kind is EventKind.CANARY_FAILED
    assert event.reason == "proceed_to_summary"


# --- housekeeping: heartbeat and targets whose dates are over ------------------------------


async def test_one_heartbeat_a_day_at_the_configured_time(harness: Harness) -> None:
    # FakeClock starts at 09:00 IST on Tue 3 Nov; the heartbeat is at 10:30 IST.
    target = make_target(id="t1", mode="notify-only")
    await harness.add(target)
    orchestrator = harness.orchestrator(
        settings_for(target, heartbeat_at="10:30"), harness.adapter(after=10**6)
    )

    await orchestrator.run(until=harness.later(2 * 24 * 60))  # two days

    beats = [e for e in harness.notifier.events if e.kind is EventKind.HEARTBEAT]
    assert [e.at.astimezone(IST).strftime("%d %H:%M") for e in beats] == ["03 10:30", "04 10:30"]
    assert beats[0].statuses[0].target_id == "t1"


async def test_targets_whose_dates_have_all_passed_stop_themselves(
    harness: Harness, fake_clock: FakeClock
) -> None:
    over = make_target(id="over", mode="notify-only", dates=["2026-11-02"])  # yesterday
    tonight = make_target(id="tonight", mode="notify-only", dates=["2026-11-03"])
    await harness.add(over, tonight)
    orchestrator = harness.orchestrator(settings_for(over, tonight), harness.adapter(after=10**6))

    await orchestrator.run(until=harness.later(60))
    assert (await harness.store.get_target("over")).state is State.PAUSED
    assert (await harness.store.get_target("tonight")).state is State.WATCHING

    await orchestrator.run(until=harness.later(24 * 60))  # past midnight IST
    assert (await harness.store.get_target("tonight")).state is State.PAUSED
    stopped = [e.target_id for e in harness.notifier.events if e.kind is EventKind.DATES_PASSED]
    assert stopped == ["over", "tonight"]


async def test_a_hold_expires_on_time_even_while_another_target_keeps_polling(
    harness: Harness,
) -> None:
    # The loop must wake for the hold's deadline, not wait for the other target's next poll.
    harness.site.set(hold_seconds=45)
    held = make_target(id="held", mode="assist", venues=["Cinema A"])
    other = make_target(id="other", mode="notify-only", venues=["Cinema B"], dates=["2026-12-19"])
    await harness.add(held, other)
    orchestrator = harness.orchestrator(settings_for(held, other), harness.adapter(after=2))

    await orchestrator.run(until=harness.later(30))

    (attempt,) = await harness.store.attempts("held")
    expired = [e for e in harness.notifier.events if e.kind is EventKind.EXPIRED]
    assert attempt.expires_at is not None
    assert [e.at for e in expired] == [attempt.expires_at]


async def test_a_restart_during_a_hold_sends_the_countdown_again(
    harness: Harness, tmp_path: Path
) -> None:
    target = make_target(id="t1", mode="assist")
    await harness.add(target)
    first = harness.orchestrator(settings_for(target), harness.adapter())
    await first.run(until=harness.later(8))  # holding, waiting for payment; then "stopped"
    await harness.store.close()

    async with await Store.open(tmp_path / "showwatch.db", clock=harness.clock) as reopened:
        second = harness.orchestrator(
            settings_for(target), harness.adapter(after=10**6), store=reopened
        )
        await second.start()

    helds = [e for e in harness.notifier.events if e.kind is EventKind.HELD]
    assert len(helds) == 2
    assert helds[0].event_id != helds[1].event_id  # its own id, so dedup lets it through (P3.4)
    assert "recovered" in helds[1].event_id
    assert helds[1].expires_at == helds[0].expires_at
    assert helds[1].seats == helds[0].seats
