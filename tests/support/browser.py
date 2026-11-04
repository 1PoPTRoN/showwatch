"""The browser half of the network lockdown (SPEC §9 layer 2).

pytest-socket only guards the Python process. Chromium is a separate process, so every request it
makes is routed through `guard`, which aborts anything whose host is not 127.0.0.1. As a backstop,
Chromium's host resolver maps every hostname except 127.0.0.1 to "not found". That does not
apply to IP literals; the route guard covers those.
"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Route

ALLOWED_HOSTS = frozenset({"127.0.0.1"})
LAUNCH_ARGS = ("--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",)


@dataclass
class BrowserSession:
    context: BrowserContext
    blocked: list[str] = field(default_factory=list)


async def lock_down(context: BrowserContext) -> list[str]:
    """Abort every request to a host other than 127.0.0.1. Returns the list of blocked URLs."""
    blocked: list[str] = []

    async def guard(route: Route) -> None:
        if urlsplit(route.request.url).hostname in ALLOWED_HOSTS:
            await route.continue_()
        else:
            blocked.append(route.request.url)
            await route.abort("blockedbyclient")

    await context.route("**/*", guard)
    return blocked
