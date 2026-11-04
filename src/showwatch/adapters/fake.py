"""Adapter for FakeShow, the local fake cinema site in tests/fakesite (SPEC §9).

Phase 6 covers availability over HTTP. Phase 7 adds the browser steps (locators, interstitials,
seat map, hold expiry).

* One availability check is exactly one HTTP request: the listing page has every open date.
  C4 spaces requests, so an adapter must never fan out per date behind the gate's back.
* Errors map to SPEC §7 types: 429 → RateLimited; 403 → Blocked("forbidden"); a CAPTCHA or
  queue page → Blocked; 5xx, network errors, a login wall or an unexpected page → TransientError.
* It refuses any base URL that is not http://127.0.0.1, so it can never talk to a real site.
"""

from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from showwatch.clock import Clock
from showwatch.config import Target
from showwatch.errors import Blocked, RateLimited, TransientError
from showwatch.models import Show, Snapshot

LISTING_PATH = "/movie/shows"


class _ListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page: str | None = None
        self.booking: str | None = None
        self.interstitial: str | None = None
        self.shows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "main":
            self.page = values.get("data-page")
            self.booking = values.get("data-booking")
            self.interstitial = values.get("data-interstitial")
        elif tag == "a" and "data-show-id" in values:
            self.shows.append(values)


class FakeSiteAdapter:
    def __init__(self, base_url: str, *, client: httpx.AsyncClient, clock: Clock) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or parts.hostname != "127.0.0.1":
            raise ValueError(
                f"the fake site adapter only talks to http://127.0.0.1, not {base_url!r}"
            )
        self._base = base_url.rstrip("/")
        self._client = client
        self._clock = clock

    async def check_availability(self, target: Target) -> Snapshot:
        try:
            response = await self._client.get(
                f"{self._base}{LISTING_PATH}", follow_redirects=True, timeout=15.0
            )
        except httpx.TransportError as exc:  # timeouts included
            raise TransientError(f"network error ({type(exc).__name__})") from None
        if response.status_code == 429:
            raise RateLimited("the site answered 429")
        if response.status_code == 403:
            raise Blocked("forbidden")
        if response.status_code != 200:
            raise TransientError(f"the site answered {response.status_code}")

        page = _ListingParser()
        page.feed(response.text)
        if page.interstitial in ("captcha", "queue"):
            raise Blocked(page.interstitial)
        if page.interstitial == "login":
            raise TransientError("the site asks to log in before showing listings")
        if page.page != "listing":
            raise TransientError(
                f"unexpected page ({page.page or 'unknown'}); has the site changed?"
            )

        wanted = set(target.dates)
        open_dates: dict[date, list[Show]] = {}
        if page.booking == "open":
            for raw in page.shows:
                show, local_day = self._show(raw, str(response.url))
                if local_day in wanted:
                    open_dates.setdefault(local_day, []).append(show)
        return Snapshot(
            target.id, self._clock.now(), {day: tuple(shows) for day, shows in open_dates.items()}
        )

    @staticmethod
    def _show(raw: dict[str, str], page_url: str) -> tuple[Show, date]:
        try:
            starts = datetime.fromisoformat(raw["data-starts-at"])
            show = Show(
                show_id=raw["data-show-id"],
                venue=raw["data-venue"],
                screen_format=raw["data-format"],
                language=raw["data-language"],
                starts_at=starts,
                min_price=int(raw["data-min-price"]),
                url=urljoin(page_url, raw.get("href", "")),
            )
        except (KeyError, ValueError) as exc:
            raise TransientError(f"could not read a show from the listing: {exc}") from None
        return show, starts.date()  # the listing's own (local) calendar date
