"""In-memory state of the fake site, changed through `POST /__control/state` (SPEC §9).

Seat layout rows use the test notation "LABEL: cells" (A = seat, T = taken, _ = gap). Seat labels
count only real seats, so in "A: AA_AA" the cell after the gap is seat A3. That is how printed
seat numbers work on real sites, and it is why a column index is not a seat number.
"""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_LAYOUT = tuple(f"{row}: {'A' * 10}_{'A' * 10}" for row in "ABCDEFGHIJKL")
INTERSTITIALS = ("captcha", "queue", "login")

# Default button and link names. `rename_locator` overrides them by step name (T7.9).
LOCATOR_NAMES = {
    "book": "Book tickets",
    "select_seats": "Select seats",
    "proceed_to_summary": "Pay ₹{total}",
    "continue_to_payment": "Continue to payment",
    "pay_now": "Pay now",
}


class ControlError(ValueError):
    """A control API request the fake site does not understand."""


@dataclass(frozen=True)
class FakeShow:
    show_id: str
    venue: str
    on: date
    at: time
    screen_format: str = "2D"
    language: str = "hindi"
    min_price: int = 300

    @property
    def starts_at(self) -> datetime:
        return datetime.combine(self.on, self.at, tzinfo=IST)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "FakeShow":
        try:
            hours, minutes = (int(p) for p in str(raw["time"]).split(":"))
            return cls(
                show_id=str(raw["show_id"]),
                venue=str(raw["venue"]),
                on=date.fromisoformat(str(raw["date"])),
                at=time(hours, minutes),
                screen_format=str(raw.get("format", "2D")),
                language=str(raw.get("language", "hindi")),
                min_price=int(raw.get("min_price", 300)),
            )
        except (KeyError, ValueError) as exc:
            raise ControlError(f"bad show {raw!r}: {exc}") from exc


@dataclass(frozen=True)
class Cell:
    row: str
    col: int
    label: str | None  # None for a gap
    blocked: bool  # 'T' in the layout


def parse_layout(rows: tuple[str, ...] | list[str]) -> list[list[Cell]]:
    grid: list[list[Cell]] = []
    for line in rows:
        label, sep, cells = line.partition(":")
        if not sep:
            raise ControlError(f"bad seat_layout row {line!r}")
        row, number, cells_out = label.strip(), 0, []
        for col, mark in enumerate(cells.strip()):
            if mark not in "AT_":
                raise ControlError(f"bad seat mark {mark!r} in {line!r}")
            if mark == "_":
                cells_out.append(Cell(row, col, None, blocked=False))
            else:
                number += 1
                cells_out.append(Cell(row, col, f"{row}{number}", blocked=mark == "T"))
        grid.append(cells_out)
    return grid


@dataclass
class Hold:
    hold_id: str
    show_id: str
    seats: tuple[str, ...]
    expires_at: datetime
    paid: bool = False


@dataclass
class SiteState:
    movie: str = "Test Movie"
    booking_open: bool = False
    shows: list[FakeShow] = field(default_factory=list)
    layout: list[list[Cell]] = field(default_factory=lambda: parse_layout(DEFAULT_LAYOUT))
    taken: set[str] = field(default_factory=set)
    interstitial: str | None = None
    status_code: int | None = None
    latency_ms: int = 0
    steal_seats_on_click: int = 0
    hold_seconds: int = 600
    rename_locator: dict[str, str] = field(default_factory=dict)
    holds: dict[str, Hold] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def update(self, data: dict[str, Any]) -> None:
        known = {
            "movie", "booking_open", "shows", "seat_layout", "taken", "interstitial",
            "status_code", "latency_ms", "steal_seats_on_click", "hold_seconds", "rename_locator",
        }  # fmt: skip
        unknown = sorted(set(data) - known)
        if unknown:
            raise ControlError(f"unknown control keys: {', '.join(unknown)}")
        if "movie" in data:
            self.movie = str(data["movie"])
        if "booking_open" in data:
            self.booking_open = bool(data["booking_open"])
        if "shows" in data:
            self.shows = [FakeShow.parse(raw) for raw in data["shows"]]
        if "seat_layout" in data:
            self.layout = parse_layout(list(data["seat_layout"]))
        if "taken" in data:
            self.taken = {str(label) for label in data["taken"]}
        if "interstitial" in data:
            value = data["interstitial"]
            if value is not None and value not in INTERSTITIALS:
                raise ControlError(f"interstitial must be one of {INTERSTITIALS} or null")
            self.interstitial = value
        if "status_code" in data:
            self.status_code = None if data["status_code"] is None else int(data["status_code"])
        if "latency_ms" in data:
            self.latency_ms = int(data["latency_ms"])
        if "steal_seats_on_click" in data:
            self.steal_seats_on_click = int(data["steal_seats_on_click"])
        if "hold_seconds" in data:
            self.hold_seconds = int(data["hold_seconds"])
        if "rename_locator" in data:
            renamed = {str(k): str(v) for k, v in dict(data["rename_locator"]).items()}
            unknown_steps = sorted(set(renamed) - set(LOCATOR_NAMES))
            if unknown_steps:
                raise ControlError(f"unknown locator names: {', '.join(unknown_steps)}")
            self.rename_locator = renamed

    # --- queries -------------------------------------------------------------------------------

    def name(self, step: str, **values: object) -> str:
        return self.rename_locator.get(step, LOCATOR_NAMES[step]).format(**values)

    def show(self, show_id: str) -> FakeShow | None:
        return next((s for s in self.shows if s.show_id == show_id), None)

    def open_dates(self) -> list[date]:
        return sorted({s.on for s in self.shows}) if self.booking_open else []

    def seat_labels(self) -> set[str]:
        return {c.label for row in self.layout for c in row if c.label}

    def unavailable(self, show_id: str, now: datetime) -> set[str]:
        held = {
            seat
            for hold in self.holds.values()
            if hold.show_id == show_id and (hold.paid or hold.expires_at > now)
            for seat in hold.seats
        }
        blocked = {c.label for row in self.layout for c in row if c.label and c.blocked}
        return self.taken | held | blocked

    def create_hold(self, show_id: str, seats: tuple[str, ...], now: datetime) -> Hold:
        hold = Hold(
            hold_id=secrets.token_hex(4),
            show_id=show_id,
            seats=seats,
            expires_at=now + timedelta(seconds=self.hold_seconds),
        )
        self.holds[hold.hold_id] = hold
        return hold


def now_utc() -> datetime:
    return datetime.now(UTC)
