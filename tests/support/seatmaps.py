"""Compact seat-map literals for tests.

One string per row, `"LABEL: cells"`, where each cell is `A` (available), `T` (taken) or `_`
(gap / aisle):

    seat_map("H: AAAA_AAAA", "J: AATT_AAAA")
"""

from showwatch.models import SeatMap, SeatStatus

_CELLS = {status.value: status for status in SeatStatus}


def seat_map(*rows: str) -> SeatMap:
    parsed: list[tuple[str, tuple[SeatStatus, ...]]] = []
    for row in rows:
        label, sep, cells = row.partition(":")
        if not sep:
            raise ValueError(f"expected 'LABEL: cells', got {row!r}")
        parsed.append((label.strip(), tuple(_CELLS[cell] for cell in cells.strip())))
    return SeatMap(rows=tuple(parsed))
