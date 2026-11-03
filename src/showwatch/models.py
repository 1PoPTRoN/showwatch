"""Domain dataclasses (SPEC §7).

The field shapes are the public interface and must not change without asking. `__post_init__`
adds guarantees later phases rely on: values are immutable, and every datetime is tz-aware.
`Attempt` and `Event` arrive with the store (Phase 3) and the notifier (Phase 5).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
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
