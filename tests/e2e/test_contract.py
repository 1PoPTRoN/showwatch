"""Phase 10: BmsAdapter in a real (headless, locked-down) browser against FakeShow (T10.2).

tests/e2e/test_executor.py runs the whole Phase 7 suite with BmsAdapter. These tests add what that
suite cannot show: the names really come from the calibration file, a listing that needs
JavaScript is read in the browser at the same polite pace, and a page off the booking site counts
as the payment boundary.
"""

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from showwatch.adapters.base import SiteAdapter
from showwatch.adapters.bms import BmsAdapter
from showwatch.adapters.calibration import Calibration, parse_calibration
from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.errors import Blocked, LocatorNotFound, RateLimited, TransientError
from showwatch.executor.executor import Executor, Outcome
from showwatch.models import SeatStatus, Snapshot
from showwatch.ratelimit import PollGate
from showwatch.state import Trigger
from showwatch.store import Store
from showwatch.watcher.diff import diff, relevant_view
from tests.support.browser import BrowserSession
from tests.support.calibration import fakeshow_calibration, fakeshow_raw, listing_url
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
RENAMED = {
    "select_seats": "Continue",
    "proceed_to_summary": "Proceed · ₹{total}",
    "continue_to_payment": "Make payment",
    "pay_now": "Confirm and pay",
}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


def renamed_calibration(base_url: str) -> Calibration:
    raw = fakeshow_raw(base_url)
    raw["steps"]["select_seats"] = {"role": "button", "name": "Continue", "navigates": True}
    raw["steps"]["proceed_to_summary"] = {
        "role": "button",
        "name_pattern": r"^Proceed · ₹\d+$",
        "navigates": True,
    }
    raw["forbidden"] = {
        "make_payment": {"role": "link", "name": "Make payment"},
        "confirm_and_pay": {"role": "button", "name": "Confirm and pay"},
    }
    return parse_calibration(raw)


async def hold_with(
    adapter: SiteAdapter,
    gate: PollGate,
    *,
    store: Store,
    site: FakeSite,
    browser: BrowserSession,
    clock: FakeClock,
    runs: Path,
) -> tuple[Outcome, Exception | None]:
    target = make_target(id="t1", mode="assist", listing_url=listing_url(site.base_url))
    await store.add_target(target)
    await store.fire("t1", Trigger.START)
    async with gate.poll():
        snapshot = await adapter.check_availability(target)
    changes = diff(Snapshot("t1", snapshot.taken_at, {}), relevant_view(snapshot, target, tz=IST))
    assert changes
    detection = await store.fire("t1", Trigger.CHANGE_DETECTED, changes=changes)
    executor = Executor(
        store=store,
        adapter=adapter,
        notifier=RecordingNotifier(),
        clock=clock,
        gate=gate,
        tz=IST,
        open_page=browser.context.new_page,
        runs_dir=runs,
        step_timeout_ms=1_500,
    )
    result = await executor.run(detection)
    return result.outcome, result.error


async def test_t10_2_the_names_come_from_the_calibration_file(
    store: Store,
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW], rename_locator=RENAMED)
    gate = PollGate(fake_clock)
    adapter = BmsAdapter(
        renamed_calibration(fakesite.base_url),
        client=client,
        clock=fake_clock,
        gate=gate,
        open_page=browser.context.new_page,
    )
    rig: dict[str, Any] = {
        "store": store,
        "site": fakesite,
        "browser": browser,
        "clock": fake_clock,
        "runs": tmp_path / "runs",
    }

    outcome, error = await hold_with(adapter, gate, **rig)

    assert outcome is Outcome.HELD, error
    paths = [e["path"] for e in fakesite.log()]
    assert not [p for p in paths if p.startswith("/payment") or p == "/pay"]
    assert paths[-1].startswith("/summary/")


async def test_t10_2_the_fake_adapter_cannot_drive_the_renamed_site(
    store: Store,
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    # The control for the test above: the same site defeats the hard-coded names.
    fakesite.set(booking_open=True, shows=[SHOW], rename_locator=RENAMED)
    gate = PollGate(fake_clock)
    adapter = FakeSiteAdapter(fakesite.base_url, client=client, clock=fake_clock)
    outcome, error = await hold_with(
        adapter,
        gate,
        store=store,
        site=fakesite,
        browser=browser,
        clock=fake_clock,
        runs=tmp_path / "runs",
    )
    assert outcome is Outcome.FAILED
    assert isinstance(error, LocatorNotFound)
    assert error.step == "choose_quantity"


# --- the browser tier ----------------------------------------------------------------------------


def browser_adapter(
    site: FakeSite,
    client: httpx.AsyncClient,
    clock: FakeClock,
    browser: BrowserSession,
    *,
    tier: str = "auto",
) -> tuple[BmsAdapter, PollGate]:
    raw = fakeshow_raw(site.base_url)
    raw["listing"]["tier"] = tier
    gate = PollGate(clock)
    adapter = BmsAdapter(
        parse_calibration(raw),
        client=client,
        clock=clock,
        gate=gate,
        open_page=browser.context.new_page,
    )
    return adapter, gate


async def test_a_listing_that_needs_javascript_is_read_in_the_browser(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW])
    adapter, gate = browser_adapter(fakesite, client, fake_clock, browser)
    target = make_target(listing_url=listing_url(fakesite.base_url))
    async with gate.poll():
        expected = await adapter.check_availability(target)  # server-rendered, over HTTP
    assert len(browser.context.pages) == 0

    fakesite.set(listing_render="script")
    fakesite.reset_log()
    fake_clock.advance(60)
    sleeps_before = len(fake_clock.sleeps)
    async with gate.poll():
        snapshot = await adapter.check_availability(target)

    assert dict(snapshot.open_dates) == dict(expected.open_dates)
    assert dict(snapshot.open_dates)[date(2026, 12, 18)][0].show_id == "fri-a-19"
    listing = [e for e in fakesite.log() if e["path"] == "/movie/shows"]
    assert len(listing) == 2  # the HTTP read that found no listing, then the browser's
    assert fake_clock.sleeps[sleeps_before:] == [20]  # the browser load waited its turn (C4)

    fakesite.reset_log()
    fake_clock.advance(60)
    async with gate.poll():
        again = await adapter.check_availability(target)
    assert dict(again.open_dates) == dict(expected.open_dates)
    assert len([e for e in fakesite.log() if e["path"] == "/movie/shows"]) == 1  # browser only
    assert len(browser.context.pages) == 1  # one listing tab, reused
    assert browser.blocked == []


@pytest.mark.parametrize("kind", ["captcha", "queue"])
async def test_the_browser_tier_pauses_on_an_interstitial(
    kind: str,
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW], listing_render="script", interstitial=kind)
    adapter, gate = browser_adapter(fakesite, client, fake_clock, browser, tier="browser")
    with pytest.raises(Blocked) as info:
        async with gate.poll():
            await adapter.check_availability(
                make_target(listing_url=listing_url(fakesite.base_url))
            )
    assert info.value.reason == kind
    (page,) = browser.context.pages
    assert page.url.endswith(f"/{kind}")  # left where it is, for the user


async def test_a_page_off_the_booking_site_counts_as_the_payment_page(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    # A payment gateway lives on another host. Leaving the calibrated site is the payment
    # boundary, whatever the page looks like (C1).
    page = await browser.context.new_page()
    await page.goto(fakesite.url("/movie"))
    here = fakeshow_calibration(fakesite.base_url)
    raw = fakeshow_raw(fakesite.base_url.replace("127.0.0.1", "localhost"))
    elsewhere = parse_calibration(raw)
    gate = PollGate(fake_clock)

    def adapter(calibration: Calibration) -> BmsAdapter:
        return BmsAdapter(
            calibration,
            client=client,
            clock=fake_clock,
            gate=gate,
            open_page=browser.context.new_page,
        )

    assert not await adapter(here).is_payment_page(page)
    assert await adapter(elsewhere).is_payment_page(page)
    raw["site"]["hosts"] = ["127.0.0.1"]
    assert not await adapter(parse_calibration(raw)).is_payment_page(page)


def adapter_from(
    raw: dict[str, Any],
    client: httpx.AsyncClient,
    clock: FakeClock,
    browser: BrowserSession,
    **options: Any,
) -> tuple[BmsAdapter, PollGate]:
    gate = PollGate(clock)
    adapter = BmsAdapter(
        parse_calibration(raw),
        client=client,
        clock=clock,
        gate=gate,
        open_page=browser.context.new_page,
        **options,
    )
    return adapter, gate


@pytest.mark.parametrize("tier", ["http", "browser"])
@pytest.mark.parametrize(
    ("listing", "state"),
    [
        pytest.param("/movie/shows?date={date:%d-%m-%Y}", {}, id="missing-page"),
        pytest.param("/movie/shows?date={date}", {"closed_date_redirect": True}, id="redirected"),
    ],
)
async def test_both_tiers_treat_missing_and_redirected_dates_as_not_open(
    tier: str,
    listing: str,
    state: dict[str, Any],
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
) -> None:
    fakesite.set(booking_open=True, shows=[{**SHOW, "date": "2026-12-19"}], **state)
    raw = fakeshow_raw(fakesite.base_url)
    raw["listing"]["tier"] = tier
    adapter, gate = adapter_from(raw, client, fake_clock, browser)
    target = make_target(listing_url=fakesite.base_url + listing)
    async with gate.poll():
        assert dict((await adapter.check_availability(target)).open_dates) == {}
    assert adapter.tier == tier


@pytest.mark.parametrize(("status", "error"), [(429, RateLimited), (403, Blocked)])
async def test_the_browser_tier_maps_status_codes(
    status: int,
    error: type[Exception],
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW], status_code=status)
    adapter, gate = browser_adapter(fakesite, client, fake_clock, browser, tier="browser")
    with pytest.raises(error):
        async with gate.poll():
            await adapter.check_availability(
                make_target(listing_url=listing_url(fakesite.base_url))
            )


async def test_a_listing_that_never_renders_is_transient(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW])
    raw = fakeshow_raw(fakesite.base_url)
    raw["listing"]["tier"] = "browser"
    raw["listing"]["ready_pattern"] = "Text this page never has"
    adapter, gate = adapter_from(raw, client, fake_clock, browser, render_timeout_ms=300)
    with pytest.raises(TransientError, match="did not finish loading"):
        async with gate.poll():
            await adapter.check_availability(
                make_target(listing_url=listing_url(fakesite.base_url))
            )


@pytest.mark.parametrize("tier", ["http", "browser"])
async def test_text_patterns_spot_an_interstitial_in_either_tier(
    tier: str,
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW])
    raw = fakeshow_raw(fakesite.base_url)
    raw["listing"]["tier"] = tier
    raw["interstitials"]["queue"] = {"text_patterns": ["Choose a show"]}  # pretend it is a queue
    adapter, gate = adapter_from(raw, client, fake_clock, browser)
    with pytest.raises(Blocked) as info:
        async with gate.poll():
            await adapter.check_availability(
                make_target(listing_url=listing_url(fakesite.base_url))
            )
    assert info.value.reason == "queue"


async def test_the_payment_page_can_be_recognised_by_its_text(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW])
    raw = fakeshow_raw(fakesite.base_url)
    raw["pages"]["payment"] = {"text_patterns": ["UPI ID"]}
    adapter, _ = adapter_from(raw, client, fake_clock, browser)
    store_page = await browser.context.new_page()
    await store_page.goto(fakesite.url("/movie"))
    assert not await adapter.is_payment_page(store_page)
    await store_page.set_content("<main><label>UPI ID <input></label></main>")
    assert await adapter.is_payment_page(store_page)


async def test_a_seat_map_that_no_longer_reads_is_a_named_locator_failure(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW])
    page = await browser.context.new_page()
    page.set_default_timeout(2_000)
    await page.goto(fakesite.url("/show/fri-a-19/seats?qty=2"))
    for attribute, value in (("col_attribute", "data-row"), ("row_attribute", "data-nothing")):
        raw = fakeshow_raw(fakesite.base_url)
        raw["seat_map"][attribute] = value
        adapter, _ = adapter_from(raw, client, fake_clock, browser)
        with pytest.raises(LocatorNotFound) as info:
            await adapter.read_seat_map(page)
        assert info.value.step == "select_seat"
    good, _ = adapter_from(fakeshow_raw(fakesite.base_url), client, fake_clock, browser)
    seat_map = await good.read_seat_map(page)
    assert len(seat_map.rows) == 12


async def test_no_readable_countdown_means_no_known_expiry(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    page = await browser.context.new_page()
    await page.goto(fakesite.url("/movie"))
    absent, no_clock = {"test_id": "no-such-timer"}, {"role": "heading"}  # the movie's title
    for timer in (absent, no_clock):
        raw = fakeshow_raw(fakesite.base_url)
        raw["hold_timer"] = timer
        adapter, _ = adapter_from(raw, client, fake_clock, browser)
        assert await adapter.read_hold_expiry(page) is None


async def test_seats_are_read_in_column_order_and_a_spent_countdown_is_no_expiry(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    adapter, _ = adapter_from(fakeshow_raw(fakesite.base_url), client, fake_clock, browser)
    page = await browser.context.new_page()
    await page.set_content(
        '<div role="grid" aria-label="Seat map">'
        '<button data-row="A" data-col="2">A3</button>'
        '<span data-row="A" data-col="1" data-gap="true"></span>'
        '<button data-row="A" data-col="0" disabled>A1</button>'
        "</div>"
        '<p data-testid="hold-timer">Complete your booking in 00:00</p>'
    )
    seat_map = await adapter.read_seat_map(page)
    assert seat_map.rows == (("A", (SeatStatus.TAKEN, SeatStatus.GAP, SeatStatus.AVAILABLE)),)
    assert await adapter.read_hold_expiry(page) is None  # the hold has already run out


async def test_the_payment_page_is_recognised_by_its_title_alone(
    fakesite: FakeSite, browser: BrowserSession, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    raw = fakeshow_raw(fakesite.base_url)
    raw["pages"]["payment"] = {"title_patterns": ["^Payment"]}
    adapter, _ = adapter_from(raw, client, fake_clock, browser)
    page = await browser.context.new_page()
    await page.set_content("<title>Payment · FakeShow</title><h1>Pay</h1>")
    assert await adapter.is_payment_page(page)
    await page.set_content("<title>Order summary · FakeShow</title>")
    assert not await adapter.is_payment_page(page)
