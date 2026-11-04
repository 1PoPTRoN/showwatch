"""Domain dataclasses (SPEC §7).

The field shapes are the public interface and must not change without asking. `__post_init__`
adds guarantees later phases rely on: values are immutable, and every datetime is tz-aware.
`Event` is what the notifier sends; Phase 5 adds the remaining kinds and their templates.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from types import MappingProxyType


def _require_aware(value: datetime, where: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{where} must be tz-aware (store UTC), got naive {value!r}")


class SeatStatus(Enum):
    AVAILABLE = "A"
    TAKEN = "T"
    GAP = "_"  # aisle / no seat


@dataclass(frozen=True)
class SeatMap:
    rows: tuple[tuple[str, tuple[SeatStatus, ...]], ...]  # row label + seats, in on-screen order

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for label, _ in self.rows:
            if label in seen:
                raise ValueError(f"SeatMap: duplicate row {label!r}")
            seen.add(label)


@dataclass(frozen=True)
class SeatBlock:
    row: str
    start_col: int  # index into the row's on-screen cells, gaps included
    n: int

    def __post_init__(self) -> None:
        if self.start_col < 0 or self.n < 1:
            raise ValueError(
                f"SeatBlock needs start_col >= 0 and n >= 1, got start_col={self.start_col}, "
                f"n={self.n}"
            )


@dataclass(frozen=True)
class Show:
    show_id: str
    venue: str
    screen_format: str  # "IMAX 2D", "4DX", "2D", ...
    language: str
    starts_at: datetime
    min_price: int  # rupees
    url: str

    def __post_init__(self) -> None:
        if not self.show_id:
            raise ValueError("Show.show_id must not be empty")
        if self.min_price < 0:
            raise ValueError(f"Show.min_price must be >= 0, got {self.min_price}")
        _require_aware(self.starts_at, "Show.starts_at")


@dataclass(frozen=True)
class Snapshot:
    target_id: str
    taken_at: datetime
    open_dates: Mapping[date, tuple[Show, ...]]  # a date absent = not open yet

    def __post_init__(self) -> None:
        _require_aware(self.taken_at, "Snapshot.taken_at")
        # A read-only copy: neither the caller nor a consumer can change a snapshot after the fact.
        object.__setattr__(self, "open_dates", MappingProxyType(dict(self.open_dates)))


class ChangeKind(Enum):
    DATE_OPENED = "date_opened"
    SHOW_ADDED = "show_added"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    target_id: str
    on_date: date
    shows: tuple[Show, ...]


@dataclass(frozen=True)
class HeldSeats:
    """Seats ShowWatch is holding (or, in a dry run, would hold), as the site labels them."""

    show: Show
    seats: tuple[str, ...]  # e.g. ("H9", "H10")


@dataclass(frozen=True)
class Attempt:
    """One detection-to-outcome cycle of a target (SPEC §3). Open while `closed_at` is None.

    The target's current state lives on the target (store.TargetRecord). An attempt records the
    cycle: the changes that opened it, the seat-hold expiry once known, and how it ended.
    """

    attempt_id: str
    target_id: str
    opened_at: datetime
    changes: tuple[Change, ...]  # everything new found by the poll that opened it
    expires_at: datetime | None = None
    closed_at: datetime | None = None
    outcome: str | None = None  # e.g. "notified", "expired", "booked"; set when closed
    held: HeldSeats | None = None  # the seats held, once SEATS_HELD

    def __post_init__(self) -> None:
        _require_aware(self.opened_at, "Attempt.opened_at")
        for name in ("expires_at", "closed_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, f"Attempt.{name}")


class EventKind(StrEnum):
    DETECTED = "detected"  # a matching opening was found; an attempt was opened
    ALREADY_OPEN = "already_open"  # the first poll found matching shows already on sale
    BLOCKED = "blocked"  # CAPTCHA, queue or block page: the target is paused (C3)
    POLL_FAILING = "poll_failing"  # availability checks keep failing
    NO_MATCH = "no_match"  # booking opened, but no seats match the rules
    DRY_RUN = "dry_run"  # dry run finished: the seats it would have chosen
    HELD = "held"  # seats held at the order summary; the user pays (C1)
    EXPIRED = "expired"  # the hold ran out
    FAILED = "failed"  # the attempt failed (seat race lost, logged out, page changed)
    HEARTBEAT = "heartbeat"  # the daily "still watching" summary
    INTERRUPTED = "interrupted"  # ShowWatch was stopped mid-selection; nothing is held
    DATES_PASSED = "dates_passed"  # every date of the target has gone by; it is paused
    CANARY_FAILED = "canary_failed"  # the read-only flow check failed: the site may have changed


@dataclass(frozen=True)
class TargetStatus:
    """One target's line in the heartbeat."""

    target_id: str
    movie: str
    state: str
    last_poll_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class Event:
    """Something to tell the user. `event_id` is the dedup key: one id is delivered at most once."""

    event_id: str
    kind: EventKind
    at: datetime
    target_id: str | None = None  # None for whole-app events (the heartbeat)
    movie: str | None = None
    attempt_id: str | None = None
    changes: tuple[Change, ...] = ()
    reason: str | None = None
    show: Show | None = None
    seats: tuple[str, ...] = ()  # seat labels as the site shows them, e.g. ("H9", "H10")
    expires_at: datetime | None = None  # seat-hold expiry
    statuses: tuple[TargetStatus, ...] = ()  # heartbeat only

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("Event.event_id must not be empty")
        _require_aware(self.at, "Event.at")
        if self.expires_at is not None:
            _require_aware(self.expires_at, "Event.expires_at")
