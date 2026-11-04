"""The real-site adapter (SPEC §7 SiteAdapter), driven only by a calibration file (Phase 10).

Nothing about the site is hard-coded: locators, the payment boundary, how listings and seat maps
read, and the hold countdown all come from the calibration (adapters/calibration.py). The
contract test (T10.2) runs the whole Phase 7 suite with this adapter against FakeShow.

Availability (each target's `listing_url` names its listing page for one date):

* C4. Every snapshot is complete (§7: a date absent = not open). The first check of a target
  reads each of its dates, every extra page paced through the gate (`PollGate.pace`). After that,
  each check re-reads only the date read longest ago: one request per check. A date's results
  are therefore at most (dates x interval) old (docs/DESIGN-NOTES.md P10.3).
* A date whose page is missing (404), or that redirects to a page without the date, is not open
  yet. That is the normal state before booking opens, not a failure (P10.4).
* Tiers: plain HTTP first, with an honest `showwatch/<version>` user agent and nothing spoofed. A
  normal page that shows no listing without JavaScript (`listing.ready_pattern` missing) switches
  this adapter to the browser for good: the same page in the user's own headed profile, at the
  same pace. A CAPTCHA, queue or block is never retried in the browser: it pauses (C3, P10.2).
* Errors map as FakeSiteAdapter's do: 429 → RateLimited; 403, CAPTCHA, queue, block page →
  Blocked; a login wall, 5xx, network or page-shape problems → TransientError.

The payment boundary (C1): a page matching `pages.payment`, or any http(s) page off the site's
hosts (a payment gateway lives elsewhere), is the payment page.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from showwatch import __version__
from showwatch.adapters.base import Interstitial, LocatorSet
from showwatch.adapters.calibration import Calibration, PageRule
from showwatch.adapters.listing import Document, parse_document, read_listing
from showwatch.clock import Clock
from showwatch.config import Target, render_listing_url
from showwatch.errors import Blocked, LocatorNotFound, RateLimited, TransientError
from showwatch.models import SeatMap, SeatStatus, Show, Snapshot
from showwatch.ratelimit import PollGate

log = logging.getLogger(__name__)

USER_AGENT = f"showwatch/{__version__}"
REQUEST_TIMEOUT_S = 15.0
RENDER_TIMEOUT_MS = 15_000

# Always checked against the page title, on top of the calibration's own patterns: a full-page
# CAPTCHA, queue or block nearly always says so in its title (C3). Kept narrow, because a movie
# title is part of most page titles.
BUILTIN_TITLES: Mapping[Interstitial, tuple[str, ...]] = {
    "captcha": (
        r"(?i)\bcaptcha\b",
        r"(?i)\bverify (that )?you(?:'re| are) (a )?human\b",
        r"(?i)\bare you a robot\b",
    ),
    "queue": (
        r"(?i)\byou(?:'re| are) (now )?in (the |a )?(virtual |online )?(queue|line)\b",
        r"(?i)\bvirtual (queue|waiting room)\b",
    ),
    "login": (),
    "forbidden": (r"(?i)^\s*(?:403\b|access denied\b|forbidden\s*$)",),
}
_KINDS: tuple[Interstitial, ...] = ("captcha", "queue", "login", "forbidden")
_ISO_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?")
_CLOCK_TEXT = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")
_PAGE_TEXT = "() => (document.body ? document.body.innerText : '')"
_READ_GRID = """(grid, a) => Array.from(grid.querySelectorAll("*"))
    .filter(c => c.hasAttribute(a.row) && c.hasAttribute(a.col))
    .map(c => ({
        row: c.getAttribute(a.row),
        col: c.getAttribute(a.col),
        gap: a.gap !== null && c.getAttribute(a.gap) === "true",
        taken: a.taken.some(t => c.hasAttribute(t) && c.getAttribute(t) !== "false"),
    }))"""
_READ_TIMER = """el => ({
    iso: (el.querySelector("time") || el).getAttribute("datetime"),
    text: el.innerText || el.textContent || "",
})"""


class _NeedsBrowser(Exception):
    """The HTTP response is a normal page, but its listing only appears with JavaScript."""


def builtin_interstitial(title: str) -> Interstitial | None:
    for kind in _KINDS:
        if any(re.search(p, title) for p in BUILTIN_TITLES[kind]):
            return kind
    return None


def countdown_seconds(iso: str | None, text: str) -> int | None:
    """Seconds left on a hold countdown: an ISO duration ("PT9M58S") or clock text ("09:58")."""
    if iso:
        match = _ISO_DURATION.fullmatch(iso.strip())
        if match is not None and any(match.groups()):
            hours, minutes, seconds = (float(g or 0) for g in match.groups())
            return int(hours * 3600 + minutes * 60 + seconds)
    found = _CLOCK_TEXT.search(text)
    if found is None:
        return None
    first, second, third = int(found[1]), int(found[2]), found[3]
    if third is not None:
        return first * 3600 + second * 60 + int(third)
    return first * 60 + second


def _blocking(kind: Interstitial | None) -> None:
    if kind in ("captcha", "queue", "forbidden"):
        raise Blocked(kind)
    if kind == "login":
        raise TransientError("the site asks to log in before showing listings: run showwatch login")


def _not_open(status: int) -> bool:
    """True if the status means "not open yet"; raises for trouble (C4)."""
    if status == 429:
        raise RateLimited("the site answered 429")
    if status == 403:
        raise Blocked("forbidden")
    if status == 404:
        return True
    if not 200 <= status < 300 and status != 304:
        raise TransientError(f"the site answered {status}")
    return False


class BmsAdapter:
    def __init__(
        self,
        calibration: Calibration,
        *,
        client: httpx.AsyncClient,
        clock: Clock,
        gate: PollGate,
        open_page: Callable[[], Awaitable[Page]],
        render_timeout_ms: int = RENDER_TIMEOUT_MS,
    ) -> None:
        self._cal = calibration
        self._client = client
        self._clock = clock
        self._gate = gate
        self._open_page = open_page
        self._render_timeout_ms = render_timeout_ms
        self._tz = ZoneInfo(calibration.site.timezone)
        self._hosts = calibration.site.all_hosts
        self._tier: Literal["http", "browser"] = (
            "browser" if calibration.listing.tier == "browser" else "http"
        )
        self._listing_page: Page | None = None
        self._reads = 0
        self._cache: dict[str, dict[date, tuple[int, tuple[Show, ...]]]] = {}

    @property
    def tier(self) -> str:
        return self._tier

    # --- availability --------------------------------------------------------------------------

    async def check_availability(self, target: Target) -> Snapshot:
        template = self._listing_url(target)
        today = self._clock.now().astimezone(self._tz).date()
        days = [day for day in target.dates if day >= today]
        cache = self._cache.setdefault(target.id, {})
        for gone in set(cache) - set(days):
            del cache[gone]
        missing = [day for day in days if day not in cache]
        todo = missing or ([min(days, key=lambda day: cache[day][0])] if days else [])
        for index, day in enumerate(todo):
            if index:
                await self._gate.pace()  # an extra request in this check: spaced like any other
            shows = await self._read_day(template, day, target)
            self._reads += 1
            cache[day] = (self._reads, shows)
        return Snapshot(
            target.id,
            self._clock.now(),
            {day: cache[day][1] for day in days if day in cache and cache[day][1]},
        )

    def _listing_url(self, target: Target) -> str:
        if target.listing_url is None:
            raise TransientError(
                f"{target.id} has no listing_url, so there is no page to check for its shows"
            )
        url, _ = render_listing_url(target.listing_url, target.dates[0])
        if (urlsplit(url).hostname or "").lower() not in self._hosts:
            raise TransientError(f"{target.id}: listing_url is not on the calibrated site")
        return target.listing_url

    async def _read_day(self, template: str, day: date, target: Target) -> tuple[Show, ...]:
        url, token = render_listing_url(template, day)
        if self._tier == "http":
            try:
                return await self._read_over_http(url, token, day, target)
            except _NeedsBrowser:
                if self._cal.listing.tier == "http":
                    raise TransientError(
                        "the listing page shows no listing without JavaScript; set listing.tier "
                        "to auto or browser in the calibration file"
                    ) from None
            log.warning("the listing page needs JavaScript: reading it in the browser from now on")
            self._tier = "browser"
            await self._gate.pace()
        return await self._read_in_browser(url, token, day, target)

    async def _read_over_http(
        self, url: str, token: str, day: date, target: Target
    ) -> tuple[Show, ...]:
        try:
            response = await self._client.get(
                url,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=REQUEST_TIMEOUT_S,
            )
        except httpx.TransportError as exc:  # timeouts included; never log the exception text
            raise TransientError(f"network error ({type(exc).__name__})") from None
        if _not_open(response.status_code):
            return ()
        doc = parse_document(response.text)
        final = str(response.url)
        _blocking(self._kind(urlsplit(final).path, doc.title, doc.text))
        if token not in final:
            return ()  # sent elsewhere: this date is not open
        if not re.search(self._cal.listing.ready_pattern, doc.text):
            raise _NeedsBrowser
        return self._shows(doc, final, day, target)

    async def _read_in_browser(
        self, url: str, token: str, day: date, target: Target
    ) -> tuple[Show, ...]:
        page = await self._page()
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=self._render_timeout_ms
            )
            if response is not None and _not_open(response.status):
                return ()
            _blocking(await self.detect_interstitial(page))
            if token not in page.url:
                return ()
            ready = page.get_by_text(re.compile(self._cal.listing.ready_pattern)).first
            try:
                await ready.wait_for(state="attached", timeout=self._render_timeout_ms)
            except PlaywrightTimeoutError:
                _blocking(await self.detect_interstitial(page))
                raise TransientError(
                    "the listing page did not finish loading (listing.ready_pattern never showed)"
                ) from None
            html = await page.content()
        except PlaywrightError as exc:
            raise TransientError(
                f"browser error reading the listing ({type(exc).__name__})"
            ) from None
        return self._shows(parse_document(html), page.url, day, target)

    def _shows(self, doc: Document, page_url: str, day: date, target: Target) -> tuple[Show, ...]:
        return read_listing(
            doc, self._cal.listing, page_url=page_url, day=day, tz=self._tz, target=target
        )

    async def _page(self) -> Page:
        if self._listing_page is None or self._listing_page.is_closed():
            self._listing_page = await self._open_page()
        return self._listing_page

    # --- pages ---------------------------------------------------------------------------------

    def _kind(self, path: str, title: str, text: str | None) -> Interstitial | None:
        found = builtin_interstitial(title)
        if found is not None:
            return found
        for kind in _KINDS:
            rule: PageRule = getattr(self._cal.interstitials, kind)
            if rule.matches_url_or_title(path=path, title=title):
                return kind
            if text is not None and rule.matches_text(text):
                return kind
        return None

    async def _page_text(self, page: Page) -> str:
        text: str = await page.evaluate(_PAGE_TEXT)
        return text

    def locators(self) -> LocatorSet:
        return self._cal.locators()

    def forbidden(self) -> LocatorSet:
        return self._cal.forbidden_locators()

    async def detect_interstitial(self, page: Page) -> Interstitial | None:
        rules = self._cal.interstitials
        wants_text = any(getattr(rules, kind).text_patterns for kind in _KINDS)
        text = await self._page_text(page) if wants_text else None
        return self._kind(urlsplit(page.url).path, await page.title(), text)

    async def is_payment_page(self, page: Page) -> bool:
        parts = urlsplit(page.url)
        # Off the booking site: a payment gateway, or somewhere ShowWatch never goes.
        if parts.scheme in ("http", "https") and (parts.hostname or "").lower() not in self._hosts:
            return True
        rule = self._cal.pages.payment
        if rule.matches_url_or_title(path=parts.path, title=await page.title()):
            return True
        return bool(rule.text_patterns) and rule.matches_text(await self._page_text(page))

    async def read_seat_map(self, page: Page) -> SeatMap:
        spec = self._cal.seat_map
        cells: list[dict[str, Any]] = (
            await spec.container.spec()
            .resolve(page)
            .evaluate(
                _READ_GRID,
                {
                    "row": spec.row_attribute,
                    "col": spec.col_attribute,
                    "gap": spec.gap_attribute,
                    "taken": list(spec.taken_attributes),
                },
            )
        )
        rows: dict[str, list[tuple[int, SeatStatus]]] = {}
        try:
            for cell in cells:
                status = (
                    SeatStatus.GAP
                    if cell["gap"]
                    else SeatStatus.TAKEN
                    if cell["taken"]
                    else SeatStatus.AVAILABLE
                )
                rows.setdefault(str(cell["row"]), []).append((int(cell["col"]), status))
        except (TypeError, ValueError):
            raise LocatorNotFound("select_seat") from None  # the seat map no longer reads
        if not rows:
            raise LocatorNotFound("select_seat")
        return SeatMap(
            rows=tuple(
                (label, tuple(status for _, status in sorted(row))) for label, row in rows.items()
            )
        )

    async def read_hold_expiry(self, page: Page) -> datetime | None:
        timer = self._cal.hold_timer.spec().resolve(page)
        if await timer.count() != 1:
            return None
        info: dict[str, Any] = await timer.evaluate(_READ_TIMER)
        seconds = countdown_seconds(info.get("iso"), str(info.get("text") or ""))
        if seconds is None or seconds <= 0:
            return None
        return self._clock.now() + timedelta(seconds=seconds)
