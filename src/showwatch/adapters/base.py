"""Adapter protocols (SPEC §7).

`AvailabilityChecker` is the part of `SiteAdapter` the watcher needs. Phase 6 adds the full
`SiteAdapter` (locators, interstitial detection, seat-map reading), which extends it.
"""

from typing import Protocol

from showwatch.config import Target
from showwatch.models import Snapshot


class AvailabilityChecker(Protocol):
    async def check_availability(self, target: Target) -> Snapshot:
        """The target's open dates and shows. Raises RateLimited, Blocked or TransientError."""
        ...
