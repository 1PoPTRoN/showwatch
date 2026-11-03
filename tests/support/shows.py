"""Shows written in IST wall-clock time (stored as UTC), matching `make_target()` by default."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from showwatch.models import Show

IST = ZoneInfo("Asia/Kolkata")
FRI = date(2026, 12, 18)  # make_target() watches this date
SAT = date(2026, 12, 19)


def show(
    show_id: str,
    *,
    on: date = FRI,
    venue: str = "Cinema A",
    fmt: str = "IMAX 2D",
    lang: str = "hindi",
    at: str = "19:00",
    price: int = 400,
) -> Show:
    hours, minutes = (int(part) for part in at.split(":"))
    starts = datetime.combine(on, time(hours, minutes), tzinfo=IST).astimezone(UTC)
    return Show(show_id, venue, fmt, lang, starts, price, f"http://127.0.0.1/show/{show_id}")
