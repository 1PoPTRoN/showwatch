"""Show choice: `rank_shows` (SPEC §7, "Show ranking (exact definition)").

Pure and deterministic. A show is kept only if all of these hold:
  * its language matches the target's;
  * its format is one of the target's formats;
  * its start, as wall-clock time in `tz`, lies inside the showtime window (both ends included);
  * its minimum price is at most `max_price_per_seat`;
  * its venue is one of the target's venues, or the target lists no venues.

Labels (language, format, venue) are compared ignoring case and spacing (`normalize_label`). Kept
shows are ordered by venue rank, format rank, start time and then show_id. The remaining fields
break any leftover tie, so the result never depends on input order, even when ids repeat.
"""

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from showwatch.config import DEFAULT_TIMEZONE, Target, normalize_label
from showwatch.models import Show

_DEFAULT_TZ = ZoneInfo(DEFAULT_TIMEZONE)


def _ranks(labels: Sequence[str]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for rank, label in enumerate(labels):
        ranks.setdefault(normalize_label(label), rank)
    return ranks


def rank_shows(shows: Sequence[Show], target: Target, *, tz: ZoneInfo = _DEFAULT_TZ) -> list[Show]:
    """The target's eligible shows, best first.

    `tz` is the timezone whose wall clock the showtime window is written in (Settings.tz). It is an
    optional addition to the SPEC §7 signature and defaults to Asia/Kolkata.
    """
    language = normalize_label(target.language)
    formats = _ranks(target.formats)
    venues = _ranks(target.venues)
    window = target.showtime_window

    def eligible(show: Show) -> bool:
        local = show.starts_at.astimezone(tz).time()
        return (
            normalize_label(show.language) == language
            and normalize_label(show.screen_format) in formats
            and window.start <= local <= window.end
            and show.min_price <= target.max_price_per_seat
            and (not venues or normalize_label(show.venue) in venues)
        )

    def order(show: Show) -> tuple[int, int, datetime, str, str, str, str, str, int]:
        return (
            venues.get(normalize_label(show.venue), 0),
            formats[normalize_label(show.screen_format)],
            show.starts_at,
            show.show_id,
            # Not part of the spec order; they only make the order total.
            show.url,
            show.venue,
            show.screen_format,
            show.language,
            show.min_price,
        )

    return sorted(filter(eligible, shows), key=order)
