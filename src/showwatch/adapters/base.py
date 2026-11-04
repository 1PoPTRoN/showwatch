"""Adapter protocols (SPEC §7).

`AvailabilityChecker` is the part the watcher needs. `SiteAdapter` extends it with what the
executor needs: named locators for each booking step, the forbidden locators (the payment
boundary), interstitial detection, and reading the seat map and the hold countdown.

A `LocatorSpec` finds an element the way §5 requires: by role and accessible name, by test id or by
visible text, never by a deep CSS or XPath chain. `{placeholders}` in a name or test id are filled
in when the locator is resolved (for example the seat at `{row}`/`{col}`).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from playwright.async_api import Locator, Page

from showwatch.config import Target
from showwatch.models import SeatMap, Snapshot

AriaRole = Any  # Playwright's role literal; kept loose so adapters can name any ARIA role


@dataclass(frozen=True)
class LocatorSpec:
    role: str | None = None
    name: str | None = None  # accessible name (exact match), may hold {placeholders}
    name_pattern: str | None = None  # or a regex for names that carry prices, e.g. r"^Pay ₹\d+"
    test_id: str | None = None
    text: str | None = None
    navigates: bool = False  # clicking it loads a new page

    def __post_init__(self) -> None:
        kinds = [k for k in (self.role, self.test_id, self.text) if k is not None]
        if len(kinds) != 1:
            raise ValueError("a LocatorSpec needs exactly one of role, test_id or text")
        if self.name is not None and self.name_pattern is not None:
            raise ValueError("give a name or a name_pattern, not both")

    def resolve(self, page: Page, **values: object) -> Locator:
        if self.test_id is not None:
            return page.get_by_test_id(self.test_id.format(**values))
        if self.text is not None:
            return page.get_by_text(self.text.format(**values), exact=True)
        role: AriaRole = self.role
        if self.name_pattern is not None:
            return page.get_by_role(role, name=re.compile(self.name_pattern))
        if self.name is not None:
            return page.get_by_role(role, name=self.name.format(**values), exact=True)
        return page.get_by_role(role)


LocatorSet = Mapping[str, LocatorSpec]
Interstitial = Literal["captcha", "queue", "login", "forbidden"]


class AvailabilityChecker(Protocol):
    async def check_availability(self, target: Target) -> Snapshot:
        """The target's open dates and shows. Raises RateLimited, Blocked or TransientError."""
        ...


class SiteAdapter(AvailabilityChecker, Protocol):
    def locators(self) -> LocatorSet:
        """Named locators for the allow-listed steps (see executor.guards.Step)."""
        ...

    def forbidden(self) -> LocatorSet:
        """Locators that must never be clicked, such as "Continue to payment"."""
        ...

    async def detect_interstitial(self, page: Page) -> Interstitial | None:
        """A CAPTCHA, queue, login wall or block page ("forbidden") on the page, or None."""
        ...

    async def is_payment_page(self, page: Page) -> bool: ...

    async def read_seat_map(self, page: Page) -> SeatMap: ...

    async def read_hold_expiry(self, page: Page) -> datetime | None:
        """When the hold on the order summary runs out, read from its countdown."""
        ...
