"""Seat choice: `best_block` (SPEC §7, "Seat scoring (exact definition)").

Pure and deterministic. For a run of `n` seats starting at cell `start` in a row of `L` cells:

    score = row_bonus - |mid - centre|,  mid = start + (n - 1) / 2,  centre = (L - 1) / 2

Internally every score is doubled so it is an exact integer, and no float rounding can ever decide
a tie:

    2 * score = 2 * row_bonus - |2 * start + n - 1 - (L - 1)|

Ties go to the earlier row on screen, then the lower column. Rows are visited in that order and a
candidate only replaces the best one if it scores strictly higher.
"""

from collections.abc import Sequence

from showwatch.models import SeatBlock, SeatMap, SeatStatus

ROW_BONUS_STEP = 10


def _row_key(label: str) -> str:
    return label.strip().upper()


def best_block(
    seat_map: SeatMap, n: int, preferred_rows: Sequence[str], avoid_rows: Sequence[str]
) -> SeatBlock | None:
    """The best block of `n` adjacent available seats, or None when no row has one.

    Row labels are compared ignoring case and surrounding spaces. An avoided row is never chosen,
    even when it is also preferred.
    """
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    avoid = {_row_key(row) for row in avoid_rows}
    bonus: dict[str, int] = {}
    for index, row in enumerate(preferred_rows):
        bonus.setdefault(_row_key(row), ROW_BONUS_STEP * (len(preferred_rows) - index))

    best: SeatBlock | None = None
    best_score = 0  # doubled; only meaningful once `best` is set
    for label, cells in seat_map.rows:
        key = _row_key(label)
        if key in avoid or len(cells) < n:
            continue
        double_bonus = 2 * bonus.get(key, 0)
        last = len(cells) - 1
        run = 0
        for col, status in enumerate(cells):
            run = run + 1 if status is SeatStatus.AVAILABLE else 0
            if run >= n:
                start = col - n + 1
                score = double_bonus - abs(2 * start + n - 1 - last)
                if best is None or score > best_score:
                    best, best_score = SeatBlock(label, start, n), score
    return best
