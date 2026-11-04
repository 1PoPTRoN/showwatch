"""Phase 6: the browser-level network lockdown (SPEC §9 layer 2; test T6.8).

pytest-socket only covers the Python process. The browser is a separate process, so the e2e
fixture routes every browser request and aborts any whose host is not 127.0.0.1.
"""

import pytest
from playwright.async_api import Error as PlaywrightError

from tests.support.browser import BrowserSession
from tests.support.fakesite import FakeSite


async def test_t6_8_requests_to_any_other_host_are_aborted(
    browser: BrowserSession, fakesite: FakeSite
) -> None:
    page = await browser.context.new_page()
    await page.goto(fakesite.url("/__control/external-ref"))
    assert await page.get_by_role("heading", name="External references").is_visible()

    blocked = " ".join(browser.blocked)
    assert "203.0.113.10" in blocked  # an IP literal: only the route guard stops it
    assert "assets.external.invalid" in blocked  # a hostname: route guard plus resolver rules


async def test_navigating_away_from_loopback_is_blocked(browser: BrowserSession) -> None:
    page = await browser.context.new_page()
    with pytest.raises(PlaywrightError, match="ERR_BLOCKED_BY_CLIENT"):
        await page.goto("http://203.0.113.10/")
    assert browser.blocked == ["http://203.0.113.10/"]


async def test_the_fake_site_itself_is_reachable(
    browser: BrowserSession, fakesite: FakeSite
) -> None:
    fakesite.set(booking_open=True)
    page = await browser.context.new_page()
    await page.goto(fakesite.url("/movie"))
    assert await page.get_by_role("link", name="Book tickets").is_visible()
    assert browser.blocked == []
