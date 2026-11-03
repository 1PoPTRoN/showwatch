"""A valid `Target` with overridable fields, for tests that need one but are not about config."""

from typing import Any

from showwatch.config import Target

_DEFAULTS: dict[str, Any] = {
    "id": "movie-fri",
    "movie": "Test Movie",
    "city": "bengaluru",
    "language": "hindi",
    "formats": ["IMAX 2D", "4DX", "2D"],
    "venues": ["Cinema A", "Cinema B"],
    "dates": ["2026-12-18"],
    "showtime_window": {"start": "18:00", "end": "22:30"},
    "seats": 2,
    "preferred_rows": ["H", "J", "K"],
    "avoid_rows": ["A", "B", "C"],
    "max_price_per_seat": 600,
}


def make_target(**overrides: Any) -> Target:
    return Target.model_validate({**_DEFAULTS, **overrides})
