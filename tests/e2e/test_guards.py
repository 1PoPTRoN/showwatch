"""Phase 7: the payment boundary (C1; test T7.3).

`safe_click` refuses on the payment page and on any forbidden locator, *before* touching the
page. FakeShow's request log proves no request followed.
"""

import re
from collections.abc import AsyncIterator

import httpx
import pytest

from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.errors import PaymentBoundaryError
from showwatch.executor.executor import Step
from showwatch.executor.guards import safe_click
from tests.support.browser import BrowserSession
from tests.support.clock import FakeClock
from tests.support.fakesite import FakeSite

SHOW = {
    "show_id": "s1",
    "venue": "Cinema A",
    "date": "2026-12-18",
    "time": "19:00",
    "min_price": 300,
}


def make_hold(site: FakeSite) -> str:
    response = httpx.post(site.url("/show/s1/hold"), data={"qty": "2", "selected": "H5,H6"})
    match = re.search(r"/summary/(\w+)", response.headers["location"])
    assert match
    return match[1]


@pytest.fixture
async def adapter(fakesite: FakeSite, fake_clock: FakeClock) -> AsyncIterator[FakeSiteAdapter]:
    fakesite.set(booking_open=True, shows=[SHOW])
    async with httpx.AsyncClient() as client:
        yield FakeSiteAdapter(fakesite.base_url, client=client, clock=fake_clock)


async def test_t7_3_no_click_is_allowed_on_the_payment_page(
    browser: BrowserSession, fakesite: FakeSite, adapter: FakeSiteAdapter
) -> None:
    hold = make_hold(fakesite)
    page = await browser.context.new_page()
    await page.goto(fakesite.url(f"/payment/{hold}"))  # the test goes there, not the executor
    fakesite.reset_log()

    pay_now = page.get_by_role("button", name="Pay now")
    with pytest.raises(PaymentBoundaryError, match="payment page"):
        await safe_click(page, pay_now, adapter, step=Step.PROCEED_TO_SUMMARY)

    assert fakesite.log() == []  # refused before anything was sent


async def test_t7_3_forbidden_locators_are_refused_anywhere(
    browser: BrowserSession, fakesite: FakeSite, adapter: FakeSiteAdapter
) -> None:
    hold = make_hold(fakesite)
    page = await browser.context.new_page()
    await page.goto(fakesite.url(f"/summary/{hold}"))
    fakesite.reset_log()

    continue_link = page.get_by_role("link", name="Continue to payment")
    with pytest.raises(PaymentBoundaryError, match="continue_to_payment"):
        await safe_click(page, continue_link, adapter, step=Step.PROCEED_TO_SUMMARY)

    assert fakesite.log() == []
    assert "/summary/" in page.url


def test_the_step_allow_list_has_no_payment_step() -> None:
    assert [s.value for s in Step] == [
        "open_show",
        "choose_quantity",
        "select_seat",
        "proceed_to_summary",
    ]
    assert not [s for s in Step if "pay" in s.value]
