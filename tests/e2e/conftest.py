"""End-to-end fixtures: a headless Chromium (allowed in tests only, C6), locked to 127.0.0.1."""

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import async_playwright

from tests.support.browser import LAUNCH_ARGS, BrowserSession, lock_down


@pytest.fixture
async def browser() -> AsyncIterator[BrowserSession]:
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True, args=list(LAUNCH_ARGS))
        try:
            context = await chromium.new_context()
            session = BrowserSession(context, await lock_down(context))
            yield session
            await context.close()
        finally:
            await chromium.close()
