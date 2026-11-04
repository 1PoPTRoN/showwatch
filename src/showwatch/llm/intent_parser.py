"""Plain language → a target (T9.1, T9.2). `showwatch parse "<text>"`.

Claude only drafts. Its answer must match `IntentDraft` (structured outputs), and is then run
through the same strict validation as a YAML file: limits, past dates judged on today's date in
IST, row labels and time formats. The result is always notify-only, so seat holding is something
the user turns on deliberately. Nothing is saved here; the CLI asks first.
"""

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict

from showwatch.clock import Clock
from showwatch.config import DEFAULT_TIMEZONE, Target, parse_settings
from showwatch.llm.client import StructuredClaude

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class IntentDraft(BaseModel):
    """What Claude fills in. Every field is required, so nothing is silently defaulted."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    movie: str
    city: str
    language: str
    formats: list[str]
    venues: list[str]
    dates: list[str]
    showtime_start: str
    showtime_end: str
    seats: int
    preferred_rows: list[str]
    avoid_rows: list[str]
    max_price_per_seat: int


def _system(today: date) -> str:
    when = f"{_DAYS[today.weekday()]} {today.day} {_MONTHS[today.month - 1]} {today.year}"
    return f"""You turn one person's request for movie tickets in India into a watch target for
ShowWatch, a personal tool that alerts them when booking opens. Today is {when} (Asia/Kolkata).

Fill every field:
- target_id: a short slug of lowercase letters, digits and hyphens, such as "dune-fri".
- movie: the title as written. city: lowercase, such as "bengaluru". language: lowercase; if the
  request does not say, use "hindi".
- formats: screen formats in order of preference, written as BookMyShow lists them ("IMAX 2D",
  "IMAX 3D", "4DX", "3D", "2D"). "IMAX" alone means ["IMAX 2D", "IMAX 3D"].
- venues: cinema names only if the request names them; otherwise [] (any cinema).
- dates: YYYY-MM-DD, worked out from today. "this Friday" is the coming Friday.
- showtime_start / showtime_end: "HH:MM" (24-hour). Morning 09:00-12:00, afternoon 12:00-17:00,
  evening 18:00-22:30, night 21:00-23:59; if no time is given, 09:00-23:59.
- seats: the number of tickets (1-10).
- preferred_rows / avoid_rows: row letters. "rows H to K" lists each letter in that range.
  Use [] if not mentioned.
- max_price_per_seat: rupees; 1000 if not mentioned.
Do not invent details the request does not imply."""


class IntentParser:
    def __init__(
        self, claude: StructuredClaude, *, clock: Clock, tz: ZoneInfo | None = None
    ) -> None:
        self._claude = claude
        self._clock = clock
        self._tz = tz or ZoneInfo(DEFAULT_TIMEZONE)

    async def parse(self, text: str) -> Target:
        """Raises LlmError (no usable answer) or ConfigError (an answer that breaks the rules)."""
        today = self._clock.now().astimezone(self._tz).date()
        draft = await self._claude.ask(
            system=_system(today), content=text, schema=IntentDraft, effort="low"
        )
        settings = parse_settings(
            {"version": 1, "timezone": self._tz.key, "targets": [_as_target(draft)]},
            clock=self._clock,
            source="the parsed request",
        )
        return settings.targets[0]


def _as_target(draft: IntentDraft) -> dict[str, Any]:
    return {
        "id": draft.target_id,
        "mode": "notify-only",  # always: holding seats is the user's explicit choice
        "movie": draft.movie,
        "city": draft.city,
        "language": draft.language,
        "formats": draft.formats,
        "venues": draft.venues,
        "dates": draft.dates,
        "showtime_window": {"start": draft.showtime_start, "end": draft.showtime_end},
        "seats": draft.seats,
        "preferred_rows": draft.preferred_rows,
        "avoid_rows": draft.avoid_rows,
        "max_price_per_seat": draft.max_price_per_seat,
    }


def target_yaml(target: Target) -> str:
    """The target as a targets.yaml entry, for the user to read before saving."""
    return yaml.safe_dump(
        {"targets": [target.model_dump(mode="json")]}, sort_keys=False, allow_unicode=True
    )
