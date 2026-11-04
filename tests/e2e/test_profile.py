"""Phase 7: the persistent browser profile (C2; test T7.12).

The user logs in once, by hand, into a saved profile. ShowWatch only reuses it: it never reads,
stores or types credentials. The profile path is treated as a secret in logs.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from showwatch import logging as swlog
from showwatch.executor.browser import open_profile
from tests.support.browser import LAUNCH_ARGS, lock_down
from tests.support.fakesite import FakeSite


@pytest.fixture
def redaction() -> Iterator[None]:
    uninstall = swlog.install_redaction()
    try:
        yield
    finally:
        uninstall()
        swlog.forget_secrets()


async def test_t7_12_cookies_survive_between_runs_and_the_path_never_logs(
    redaction: None, fakesite: FakeSite, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    profile = tmp_path / "secret-profile-location"
    caplog.set_level(logging.DEBUG)

    async with async_playwright() as playwright:
        first = await open_profile(playwright, profile, headless=True, args=LAUNCH_ARGS)
        await lock_down(first)
        page = await first.new_page()
        await page.goto(fakesite.url("/movie"))
        cookie = next(c for c in await first.cookies() if c["name"] == "fakeshow_session")
        await first.close()

        second = await open_profile(playwright, profile, headless=True, args=LAUNCH_ARGS)
        await lock_down(second)
        again = {c["name"]: c["value"] for c in await second.cookies()}
        await second.close()

    assert again.get("fakeshow_session") == cookie["value"]
    assert any(r.name.startswith("showwatch") for r in caplog.records)  # something was logged
    assert str(profile) not in caplog.text
    for record in caplog.records:
        assert str(profile) not in record.getMessage()


def test_the_executor_browser_is_headed_unless_told_otherwise() -> None:
    import inspect

    assert inspect.signature(open_profile).parameters["headless"].default is False  # C6
