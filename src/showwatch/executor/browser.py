"""The executor's browser: one persistent profile, headed by default (C2, C6).

The user logs in once, by hand, in a headed window (`showwatch login`). ShowWatch reuses the saved
profile and never reads, stores or types credentials. The profile's path is registered as a
secret, so it never appears in a log line (T7.12).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from showwatch.logging import register_secret

log = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path.home() / ".showwatch" / "profile"


def _prepare(profile_dir: Path) -> None:
    for form in {str(profile_dir), str(profile_dir.resolve())}:
        register_secret(form)
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.chmod(0o700)  # it holds the user's session cookies


async def open_profile(
    playwright: Playwright,
    profile_dir: Path,
    *,
    headless: bool = False,  # C6: headed unless a test asks otherwise
    args: Sequence[str] = (),
) -> BrowserContext:
    """Open (creating if needed) the saved browser profile at `profile_dir`."""
    await asyncio.to_thread(_prepare, profile_dir)
    log.debug("opening browser profile at %s", profile_dir)  # redacted: the path is a secret
    log.info("opening the saved browser profile (%s)", "headless" if headless else "headed")
    return await playwright.chromium.launch_persistent_context(
        str(profile_dir), headless=headless, args=list(args)
    )


class BrowserHolder:
    """Opens the saved profile on first use and keeps it warm (SPEC §1: a warm session is fast).

    Nothing is launched until the first `new_page()`, so a notify-only target never starts a
    browser (T8.1). Headed by default (C6). `on_context` lets tests add the network lockdown.
    """

    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        args: Sequence[str] = (),
        on_context: Callable[[BrowserContext], Awaitable[object]] | None = None,
    ) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._args = tuple(args)
        self._on_context = on_context
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    @property
    def launched(self) -> bool:
        return self._context is not None

    async def new_page(self) -> Page:
        if self._context is None:
            self._playwright = await async_playwright().start()
            self._context = await open_profile(
                self._playwright, self._profile_dir, headless=self._headless, args=self._args
            )
            if self._on_context is not None:
                await self._on_context(self._context)
        return await self._context.new_page()

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
