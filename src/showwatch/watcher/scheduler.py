"""Poll interval maths (SPEC §3 Scheduler, §8 polling). Pure apart from the injected RNG.

For one target, the delay until its next poll is:
  1. nominal: the hot interval inside a hot window, otherwise the base interval. Outside a window,
     if the next window starts sooner than that, wake at its start (docs/DESIGN-NOTES.md P4.3).
  2. backoff: the nominal interval times the backoff multiplier, capped at MAX_BACKOFF_S (but never
     shorter than nominal). There is no early hot-window wake while backing off.
  3. jitter: +/- jitter_pct, uniformly at random (not applied to an early wake).
  4. floor: never less than MIN_TARGET_INTERVAL_S (C4).

Hot windows are wall-clock times in the config timezone: start included, end excluded.
"""

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from showwatch import limits
from showwatch.config import Polling

_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def in_hot_window(moment: datetime, polling: Polling, tz: ZoneInfo) -> bool:
    local = moment.astimezone(tz)
    day, clock_time = _DAY_NAMES[local.weekday()], local.time()
    return any(
        day in window.days and window.start <= clock_time < window.end
        for window in polling.hot_windows
    )


def seconds_to_next_hot_window(moment: datetime, polling: Polling, tz: ZoneInfo) -> float | None:
    """Seconds until the next hot window starts (strictly after `moment`), within a week."""
    local = moment.astimezone(tz)
    starts = [
        datetime.combine(local.date() + timedelta(days=offset), window.start, tzinfo=tz)
        for offset in range(8)
        for window in polling.hot_windows
        if _DAY_NAMES[(local.date() + timedelta(days=offset)).weekday()] in window.days
    ]
    upcoming = [start for start in starts if start > moment]
    return (min(upcoming) - moment).total_seconds() if upcoming else None


def next_delay(
    now: datetime, polling: Polling, tz: ZoneInfo, rng: random.Random, *, multiplier: int = 1
) -> float:
    """Seconds until a target's next poll. Always at least MIN_TARGET_INTERVAL_S."""
    hot = in_hot_window(now, polling, tz)
    nominal = float(polling.hot_interval_s if hot else polling.base_interval_s)
    if multiplier > 1:
        interval = min(nominal * multiplier, max(float(limits.MAX_BACKOFF_S), nominal))
    else:
        until_hot = None if hot else seconds_to_next_hot_window(now, polling, tz)
        if until_hot is not None and until_hot < nominal:
            return max(float(limits.MIN_TARGET_INTERVAL_S), until_hot)
        interval = nominal
    if polling.jitter_pct:
        spread = polling.jitter_pct / 100
        interval *= 1 + rng.uniform(-spread, spread)
    return max(float(limits.MIN_TARGET_INTERVAL_S), interval)
