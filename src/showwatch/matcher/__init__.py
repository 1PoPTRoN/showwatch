"""Pure, deterministic choice of shows and seats (SPEC §7). No I/O and no clock (SPEC §5)."""

from showwatch.matcher.seats import best_block
from showwatch.matcher.shows import rank_shows

__all__ = ["best_block", "rank_shows"]
