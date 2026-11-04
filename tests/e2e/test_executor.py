"""Phase 7: the executor end to end against FakeShow (tests T7.1, T7.2, T7.4-T7.11).

A real headless Chromium (allowed in tests only, C6) drives the local fake site. Every
assertion about traffic comes from FakeShow's own request log.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.errors import LocatorNotFound
from showwatch.executor.executor import Executor, Outcome, Step
from showwatch.matcher import best_block
from showwatch.models import EventKind, SeatMap, SeatStatus, Snapshot
from showwatch.notify.messages import render
from showwatch.ratelimit import PollGate
from showwatch.state import State, Trigger
from showwatch.store import Store, TransitionResult
from showwatch.watcher.diff import diff, relevant_view
from tests.fakesite.state import DEFAULT_LAYOUT, parse_layout
from tests.support.browser import BrowserSession
from tests.support.clock import FakeClock
from tests.support.fakesite import FakeSite
from tests.support.notify import RecordingNotifier
from tests.support.targets import make_target

IST = ZoneInfo("Asia/Kolkata")
SHOW = {
    "show_id": "fri-a-19",
    "venue": "Cinema A",
    "date": "2026-12-18",
    "time": "19:00",
    "format": "IMAX 2D",
    "min_price": 450,
}
PREFERRED, AVOID = ("H", "J", "K"), ("A", "B", "C")  # make_target() defaults


def expected_labels(taken: Sequence[str] = (), n: int = 2) -> tuple[str, ...]:
    """What best_block picks on FakeShow's default layout, as printed seat labels."""
    grid = parse_layout(DEFAULT_LAYOUT)
    seat_map = SeatMap(
        rows=tuple(
            (
                row[0].row,
                tuple(
                    SeatStatus.GAP
                    if c.label is None
                    else SeatStatus.TAKEN
                    if c.label in taken
                    else SeatStatus.AVAILABLE
                    for c in row
                ),
            )
            for row in grid
        )
    )
    block = best_block(seat_map, n, PREFERRED, AVOID)
    assert block is not None
    cells = next(row for row in grid if row[0].row == block.row)
    return tuple(c.label for c in cells[block.start_col : block.start_col + n] if c.label)


class Rig:
    def __init__(
        self,
        *,
        store: Store,
        fakesite: FakeSite,
        browser: BrowserSession,
        clock: FakeClock,
        client: httpx.AsyncClient,
        runs: Path,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        step_timeout_ms: int = 5_000,
    ) -> None:
        self.store = store
        self.site = fakesite
        self.notifier = RecordingNotifier()
        self.adapter = FakeSiteAdapter(fakesite.base_url, client=client, clock=clock)
        self.runs = runs
        self.gate = PollGate(clock)
        self.executor = Executor(
            store=store,
            adapter=self.adapter,
            notifier=self.notifier,
            clock=clock,
            gate=self.gate,
            tz=IST,
            open_page=browser.context.new_page,
            runs_dir=runs,
            step_timeout_ms=step_timeout_ms,
            on_step=on_step,
        )

    async def detect(self, mode: str = "assist", target_id: str = "t1") -> TransitionResult:
        target = make_target(id=target_id, mode=mode)
        await self.store.add_target(target)
        await self.store.fire(target_id, Trigger.START)
        snapshot = await self.adapter.check_availability(target)
        empty = Snapshot(target_id, snapshot.taken_at, {})
        changes = diff(empty, relevant_view(snapshot, target, tz=IST))
        assert changes, "the fake site offers nothing that matches the target"
        return await self.store.fire(target_id, Trigger.CHANGE_DETECTED, changes=changes)

    def requests(self) -> list[tuple[str, str]]:
        return [(e["method"], e["path"]) for e in self.site.log()]


@pytest.fixture
async def client() -> Any:
    async with httpx.AsyncClient() as http:
        yield http


@pytest.fixture
def rig_factory(
    store: Store,
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> Callable[..., Rig]:
    fakesite.set(booking_open=True, shows=[SHOW])

    def build(**options: Any) -> Rig:
        return Rig(
            store=store,
            fakesite=fakesite,
            browser=browser,
            clock=fake_clock,
            client=client,
            runs=tmp_path / "runs",
            **options,
        )

    return build


# --- T7.1, T7.2, T7.8, T7.11: the happy path ----------------------------------------------


async def test_t7_1_assist_reaches_the_order_summary_with_the_best_seats(
    rig_factory: Callable[..., Rig], fake_clock: FakeClock
) -> None:
    rig = rig_factory()
    detection = await rig.detect()
    rig.site.reset_log()

    started = time.perf_counter()
    result = await rig.executor.run(detection)
    elapsed = time.perf_counter() - started

    assert result.outcome is Outcome.HELD, result.error
    assert result.held is not None
    assert result.held.seats == expected_labels()  # exactly what best_block chose
    assert result.held.show.show_id == "fri-a-19"
    assert (await rig.store.get_target("t1")).state is State.AWAITING_PAYMENT
    attempt = await rig.store.get_attempt(detection.attempt_id or "")
    assert attempt.held == result.held
    assert elapsed < 10, f"took {elapsed:.1f} s"

    # T7.11: the countdown on the summary page is stored as the hold expiry.
    assert attempt.expires_at is not None
    left = (attempt.expires_at - fake_clock.now()).total_seconds()
    assert 590 <= left <= 600

    # The user is told: seats, venue, show time, expiry.
    (held,) = [e for e in rig.notifier.events if e.kind is EventKind.HELD]
    assert held.seats == result.held.seats
    assert held.expires_at == attempt.expires_at
    assert "showwatch done" in render(held, tz=IST, html=False)

    # The browser is left on the order summary for the user to pay.
    assert result.page is not None
    assert not result.page.is_closed()
    assert "/summary/" in result.page.url


async def test_t7_2_payment_was_never_touched(rig_factory: Callable[..., Rig]) -> None:
    # C1 proof: zero POSTs to /pay and zero GETs of a payment page in FakeShow's own log.
    rig = rig_factory()
    detection = await rig.detect()
    rig.site.reset_log()
    result = await rig.executor.run(detection)
    assert result.outcome is Outcome.HELD
    requests = rig.requests()
    assert ("POST", "/pay") not in requests
    assert not [r for r in requests if r[1].startswith("/payment")]
    assert requests[-1][1].startswith("/summary/")  # nothing after the order summary


async def test_t7_8_one_numbered_screenshot_per_step(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    detection = await rig.detect()
    result = await rig.executor.run(detection)
    folder = rig.runs / (detection.attempt_id or "")
    names = sorted(p.name for p in folder.glob("*.png"))
    assert names == [
        "01-open_show.png",
        "02-choose_quantity.png",
        "03-select_seat.png",
        "04-select_seat.png",
        "05-proceed_to_summary.png",
    ]
    assert [p.name for p in result.screenshots] == names
    assert all(p.stat().st_size > 1000 for p in folder.glob("*.png"))


# --- T7.4: losing the seat race ---------------------------------------------------------------


async def test_t7_4_two_lost_races_then_the_next_best_block(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory()
    rig.site.set(steal_seats_on_click=2)
    detection = await rig.detect()

    result = await rig.executor.run(detection)

    assert result.outcome is Outcome.HELD, result.error
    first = expected_labels()
    second = expected_labels(taken=[first[0]])
    third = expected_labels(taken=[first[0], second[0]])
    assert result.held is not None
    assert result.held.seats == third
    assert len({first, second, third}) == 3


async def test_t7_4_three_lost_races_fail_the_attempt(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    rig.site.set(steal_seats_on_click=3)
    detection = await rig.detect()

    result = await rig.executor.run(detection)

    assert result.outcome is Outcome.FAILED
    assert (await rig.store.get_target("t1")).state is State.FAILED
    assert [r.reason for r in await rig.store.audit("t1")][-1] == "seat_race_exhausted"
    (event,) = [e for e in rig.notifier.events if e.kind is EventKind.FAILED]
    assert event.reason == "seat_race_exhausted"


# --- T7.5 - T7.7: interstitials -------------------------------------------------------------


@pytest.mark.parametrize("kind", ["captcha", "queue"])
async def test_t7_5_t7_6_interstitial_mid_flow_pauses_and_stops_all_requests(
    kind: str, rig_factory: Callable[..., Rig]
) -> None:
    holder: list[Rig] = []

    async def trip_at_proceed(step: Step) -> None:
        if step is Step.PROCEED_TO_SUMMARY:
            holder[0].site.set(interstitial=kind)

    rig = rig_factory(on_step=trip_at_proceed)
    holder.append(rig)
    detection = await rig.detect()
    rig.site.reset_log()

    result = await rig.executor.run(detection)

    assert result.outcome is Outcome.BLOCKED
    assert (await rig.store.get_target("t1")).state is State.PAUSED
    requests = rig.requests()
    assert requests[-1] == ("GET", f"/{kind}")  # nothing at all after the interstitial
    (event,) = [e for e in rig.notifier.events if e.kind is EventKind.BLOCKED]
    assert event.reason == kind
    assert result.page is not None
    assert not result.page.is_closed()  # left exactly where it is, for the user
    assert result.page.url.endswith(f"/{kind}")


async def test_t7_7_login_wall_fails_with_needs_login(rig_factory: Callable[..., Rig]) -> None:
    holder: list[Rig] = []

    async def trip_at_quantity(step: Step) -> None:
        if step is Step.CHOOSE_QUANTITY:
            holder[0].site.set(interstitial="login")

    rig = rig_factory(on_step=trip_at_quantity)
    holder.append(rig)
    detection = await rig.detect()

    result = await rig.executor.run(detection)

    assert result.outcome is Outcome.FAILED
    assert (await rig.store.get_target("t1")).state is State.FAILED
    (event,) = [e for e in rig.notifier.events if e.kind is EventKind.FAILED]
    assert event.reason == "needs_login"
    assert "showwatch login" in render(event, tz=IST, html=False)


# --- T7.9: the site changed ----------------------------------------------------------------


async def test_t7_9_renamed_proceed_button_is_a_named_locator_failure(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory(step_timeout_ms=1_500)
    rig.site.set(rename_locator={"proceed_to_summary": "Book now"})
    detection = await rig.detect()

    result = await rig.executor.run(detection)

    assert result.outcome is Outcome.FAILED
    assert isinstance(result.error, LocatorNotFound)
    assert result.error.step == "proceed_to_summary"
    assert (await rig.store.get_target("t1")).state is State.FAILED
    folder = rig.runs / (detection.attempt_id or "")
    assert any(p.name.endswith("-proceed_to_summary-not-found.png") for p in folder.glob("*.png"))
    (event,) = [e for e in rig.notifier.events if e.kind is EventKind.FAILED]
    assert event.reason == "locator_not_found"


# --- T7.10: dry run --------------------------------------------------------------------------


async def test_t7_10_dry_run_highlights_seats_and_clicks_none(
    rig_factory: Callable[..., Rig],
) -> None:
    rig = rig_factory()
    detection = await rig.detect(mode="dry-run")
    rig.site.reset_log()

    result = await rig.executor.run(detection)

    assert result.outcome is Outcome.DRY_RUN
    assert result.held is not None
    assert result.held.seats == expected_labels()
    shot = rig.runs / (detection.attempt_id or "") / "03-dry_run.png"
    assert shot.is_file()
    assert shot.stat().st_size > 1000
    assert not [r for r in rig.requests() if r[0] == "POST"]  # no seat-select, no hold
    assert (await rig.store.get_target("t1")).state is State.WATCHING
    (event,) = [e for e in rig.notifier.events if e.kind is EventKind.DRY_RUN]
    assert event.seats == result.held.seats


# --- and when nothing fits -----------------------------------------------------------------


async def test_no_matching_seats_goes_back_to_watching(rig_factory: Callable[..., Rig]) -> None:
    rig = rig_factory()
    full = [f"{row}: {'T' * 10}_{'T' * 10}" for row in "ABCDEFGHIJKL"]
    rig.site.set(seat_layout=full)
    detection = await rig.detect()

    result = await rig.executor.run(detection)

    assert result.outcome is Outcome.NO_MATCH
    assert (await rig.store.get_target("t1")).state is State.WATCHING
    assert [e.kind for e in rig.notifier.events] == [EventKind.NO_MATCH]
    assert not [r for r in rig.requests() if r[0] == "POST"]


async def test_polling_waits_while_seats_are_being_chosen(rig_factory: Callable[..., Rig]) -> None:
    # P4.6: the executor holds the request gate for its whole run, so no poll can interleave.
    import asyncio

    order: list[str] = []
    polls: list[asyncio.Task[None]] = []
    holder: list[Rig] = []

    async def poll() -> None:
        async with holder[0].gate.poll():
            order.append("poll")

    async def start_a_poll(step: Step) -> None:
        if step is Step.OPEN_SHOW and not polls:
            polls.append(asyncio.create_task(poll()))

    rig = rig_factory(on_step=start_a_poll)
    holder.append(rig)
    result = await rig.executor.run(await rig.detect())
    order.append("executor finished")
    await polls[0]
    assert result.outcome is Outcome.HELD
    assert order == ["executor finished", "poll"]


async def test_a_crash_right_after_holding_still_records_when_the_hold_ends(
    rig_factory: Callable[..., Rig],
) -> None:
    # If ShowWatch dies between SEATS_HELD and SUMMARY_REACHED, recovery finds the target HELD.
    # It can keep the hold running (and re-send the countdown) only if the expiry is already
    # stored; otherwise it must treat the hold as expired (docs/DESIGN-NOTES.md P3.7).
    rig = rig_factory()
    detection = await rig.detect()
    rig.notifier.fail = True  # the HELD alert is where this simulated crash happens

    with pytest.raises(RuntimeError, match="notifier is down"):
        await rig.executor.run(detection)

    assert (await rig.store.get_target("t1")).state is State.HELD
    attempt = await rig.store.get_attempt(detection.attempt_id or "")
    assert attempt.expires_at is not None
    assert attempt.held is not None
