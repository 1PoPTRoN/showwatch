"""The executor's browser: one persistent profile, headed by default (C2, C6).

The user logs in once, by hand, in a headed window (`showwatch login`). ShowWatch reuses the saved
profile and never reads, stores or types credentials. The profile's path is registered as a
secret, so it never appears in a log line (T7.12).
"""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright

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
