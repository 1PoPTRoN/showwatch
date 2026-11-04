"""Phase 10: the calibration recorder end to end, on FakeShow (headless, locked down).

A scripted "user" clicks through the booking flow while the real recording session listens. The
recorder itself never clicks, types or navigates (except opening the start page). Its draft must
need only what the user alone can say (the listing's ready marker), and then drive BmsAdapter.
"""

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.async_api import Page

from showwatch.adapters.base import LocatorSpec
from showwatch.adapters.bms import BmsAdapter
from showwatch.adapters.calibration import parse_calibration
from showwatch.errors import CalibrationError
from showwatch.ratelimit import PollGate
from showwatch.recorder import Recording, draft_yaml, record_flow
from tests.support.browser import BrowserSession
from tests.support.calibration import fakeshow_calibration, listing_url
from tests.support.clock import FakeClock
from tests.support.fakesite import FakeSite
from tests.support.targets import make_target

SHOW = {
    "show_id": "fri-a-19",
    "venue": "Cinema A",
    "date": "2026-12-18",
    "time": "19:00",
    "format": "IMAX 2D",
    "min_price": 450,
}


class ScriptedUser:
    """Plays the user's part: does what each prompt asks, in the browser, and says yes."""

    def __init__(self, site: FakeSite, page: list[Page]) -> None:
        self.site = site
        self.page = page
        self.prompts: list[str] = []
        self.questions: list[str] = []

    async def say(self, step: str, text: str) -> None:
        self.prompts.append(step)
        page = self.page[0] if self.page else None
        if step == "listing":
            assert page is not None
            async with page.expect_navigation():  # a stray link on the way: not a show time
                await page.get_by_role("link", name="Book tickets").click()
            await page.goto(self.site.url("/movie/shows?date=2026-12-18"))
            async with page.expect_navigation():
                await page.get_by_role("link", name="7:00 PM · IMAX 2D · from ₹450").click()
        elif step == "choose_quantity":
            assert page is not None
            await page.get_by_role("combobox", name="Number of seats").select_option("2")
        elif step == "select_seats":
            assert page is not None
            async with page.expect_navigation():
                await page.get_by_role("button", name="Select seats").click()
        elif step == "seat":
            assert page is not None
            async with page.expect_navigation():
                await page.get_by_test_id("seat-H-3").click()
        elif step == "proceed_to_summary":
            assert page is not None
            async with page.expect_navigation():
                await page.get_by_test_id("seat-H-4").click()  # the rest of the seats: ignored
            async with page.expect_navigation():
                await page.get_by_role("button", name="Pay ₹900").click()
        elif step == "hold_timer":
            assert page is not None
            await page.get_by_test_id("hold-timer").click()
        elif step == "continue_to_payment":
            assert page is not None
            async with page.expect_navigation():
                await page.get_by_role("link", name="Continue to payment").click()

    async def confirm(self, question: str) -> bool:
        self.questions.append(question)
        return True


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


async def record(fakesite: FakeSite, browser: BrowserSession) -> tuple[Recording, ScriptedUser]:
    pages: list[Page] = []

    def opened(page: Page) -> None:  # Playwright cannot wrap a builtin such as list.append
        pages.append(page)

    browser.context.on("page", opened)
    user = ScriptedUser(fakesite, pages)
    recording = await record_flow(browser.context, fakesite.url("/movie"), user, step_timeout_s=20)
    return recording, user


async def test_t10_3_recording_the_flow_produces_a_working_calibration(
    fakesite: FakeSite,
    browser: BrowserSession,
    fake_clock: FakeClock,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    fakesite.set(booking_open=True, shows=[SHOW], taken=["H1"])  # so a taken seat is on the map
    recording, user = await record(fakesite, browser)

    assert user.prompts == [
        "listing",
        "choose_quantity",
        "select_seats",
        "seat",
        "proceed_to_summary",
        "hold_timer",
        "continue_to_payment",
        "done",
    ]
    assert len(user.questions) == 2  # the order summary, then the payment page: user confirms
    assert recording.listing_url == fakesite.url("/movie/shows?date=2026-12-18")
    assert recording.hosts == ("127.0.0.1",)

    text = draft_yaml(recording, recorded_on=date(2026, 11, 3))
    assert f"{fakesite.base_url}/movie/shows?date={{date:%Y-%m-%d}}" in text  # listing_url hint
    raw = yaml.safe_load(text)
    with pytest.raises(CalibrationError) as info:
        parse_calibration(raw)
    assert info.value.problems == ["listing.ready_pattern: required key is missing"]

    raw["listing"]["ready_pattern"] = "Choose a show"
    recorded = parse_calibration(raw)
    expected = fakeshow_calibration(fakesite.base_url)
    assert recorded.locators() == expected.locators()
    assert recorded.forbidden_locators()["continue_to_payment"] == LocatorSpec(
        role="link", name="Continue to payment"
    )
    assert recorded.seat_map == expected.seat_map

    # And it works: the recorded file reads FakeShow's listing like the hand-written one.
    gate = PollGate(fake_clock)
    target = make_target(listing_url=listing_url(fakesite.base_url))

    async def no_page() -> Page:
        raise AssertionError("not needed")

    async with gate.poll():
        snapshot = await BmsAdapter(
            recorded, client=client, clock=fake_clock, gate=gate, open_page=no_page
        ).check_availability(target)
    assert [s.show_id for s in snapshot.open_dates[date(2026, 12, 18)]] == ["fri-a-19"]


async def test_the_recorder_sends_nothing_itself(
    fakesite: FakeSite, browser: BrowserSession
) -> None:
    # Every request in FakeShow's log is one the user caused, plus the start page.
    fakesite.set(booking_open=True, shows=[SHOW])
    await record(fakesite, browser)
    log = [(e["method"], e["path"]) for e in fakesite.log()]
    assert log[0] == ("GET", "/movie")
    assert ("POST", "/pay") not in log
    assert log[-1][1].startswith("/payment/")  # nothing after the payment page
    assert browser.blocked == []
