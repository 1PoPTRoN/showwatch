"""Phase 2: `best_block` seat choice (SPEC §7 "Seat scoring", tests T2.1-T2.9).

Columns are on-screen cell indices with gaps included. In a row of 20, the centre is 9.5.
"""

import random
import time
from collections.abc import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from showwatch.matcher import best_block
from showwatch.models import SeatBlock, SeatMap, SeatStatus
from tests.support.seatmaps import seat_map

A, T, G = SeatStatus.AVAILABLE, SeatStatus.TAKEN, SeatStatus.GAP
FREE_20 = "A" * 20


def oracle(
    seats: SeatMap, n: int, preferred: Sequence[str], avoid: Sequence[str]
) -> SeatBlock | None:
    """The §7 definition, transcribed literally (floats, brute force)."""
    best: SeatBlock | None = None
    best_key: tuple[float, int, int] | None = None
    for row_index, (label, cells) in enumerate(seats.rows):
        if label in avoid:
            continue
        bonus = 10 * (len(preferred) - preferred.index(label)) if label in preferred else 0
        centre = (len(cells) - 1) / 2
        for start in range(len(cells) - n + 1):
            if all(cell is A for cell in cells[start : start + n]):
                score = bonus - abs(start + (n - 1) / 2 - centre)
                key = (-score, row_index, start)
                if best_key is None or key < best_key:
                    best, best_key = SeatBlock(label, start, n), key
    return best


# --- T2.1 - T2.7: the definition, case by case -----------------------------------------------


def test_t2_1_free_row_of_20_puts_a_pair_in_the_centre() -> None:
    assert best_block(seat_map(f"H: {FREE_20}"), 2, [], []) == SeatBlock("H", 9, 2)


@pytest.mark.parametrize("blocker", ["_", "T"], ids=["gap", "taken"])
def test_t2_2_block_never_spans_a_gap_or_taken_seat(blocker: str) -> None:
    # The ideal pair (cols 9-10) would straddle col 9. Next best: cols 10-11 (distance 1).
    seats = seat_map(f"H: {'A' * 9}{blocker}{'A' * 10}")
    block = best_block(seats, 2, [], [])
    assert block == SeatBlock("H", 10, 2)
    cells = seats.rows[0][1][block.start_col : block.start_col + block.n]
    assert all(cell is A for cell in cells)


def test_t2_3_preferred_row_beats_a_central_block_in_an_ordinary_row() -> None:
    # H (preferred) only has room 7 cells off-centre: score 10 - 7 = 3. G is free at the centre: 0.
    seats = seat_map(f"G: {FREE_20}", f"H: AAAA{'T' * 16}")
    assert best_block(seats, 2, ["H"], []) == SeatBlock("H", 2, 2)


def test_t2_3_crossover_beyond_the_bonus_is_documented() -> None:
    # The exact formula: one preferred row is worth 10 cells of centring. Here the only free pair in
    # H is 19 cells off-centre (score -9), so the central pair in G (score 0) wins. Kept on purpose:
    # the user agreed to keep the §7 formula (docs/DESIGN-NOTES.md P2.1).
    seats = seat_map(f"G: {'A' * 40}", f"H: AA{'T' * 38}")
    assert best_block(seats, 2, ["H"], []) == SeatBlock("G", 19, 2)


def test_t2_4_earlier_listed_preferred_row_wins_at_equal_centrality() -> None:
    # H comes first on screen, but K is listed first in preferred_rows (bonus 20 vs 10).
    seats = seat_map(f"H: {FREE_20}", f"K: {FREE_20}")
    assert best_block(seats, 2, ["K", "H"], []) == SeatBlock("K", 9, 2)


def test_ties_go_to_the_earlier_row_on_screen() -> None:
    seats = seat_map(f"D: {FREE_20}", f"E: {FREE_20}")
    assert best_block(seats, 2, [], []) == SeatBlock("D", 9, 2)


def test_ties_within_a_row_go_to_the_lower_column() -> None:
    # n=1 in a row of 20: cols 9 and 10 are both 0.5 from the centre.
    assert best_block(seat_map(f"H: {FREE_20}"), 1, [], []) == SeatBlock("H", 9, 1)


def test_t2_5_no_contiguous_run_returns_none() -> None:
    seats = seat_map("H: AATAA_AA", "J: TATATA")
    assert best_block(seats, 3, [], []) is None


def test_t2_6_single_seat() -> None:
    assert best_block(seat_map("H: AAAAA"), 1, [], []) == SeatBlock("H", 2, 1)


def test_t2_6_whole_row() -> None:
    assert best_block(seat_map("H: AAAAA"), 5, [], []) == SeatBlock("H", 0, 5)


@pytest.mark.parametrize("row", ["TAAAA", "AA_AA", "AAAAT"])
def test_t2_6_whole_row_with_any_unavailable_seat_returns_none(row: str) -> None:
    assert best_block(seat_map(f"H: {row}"), 5, [], []) is None


def test_t2_7_avoided_row_is_never_chosen() -> None:
    seats = seat_map("A: TTTTTTTT", f"B: {FREE_20}")
    assert best_block(seats, 2, [], ["B"]) is None
    # An avoided row loses even to a worse row, and even when it is also "preferred".
    seats = seat_map(f"B: {FREE_20}", f"C: AA{'T' * 18}")
    assert best_block(seats, 2, ["B"], ["B"]) == SeatBlock("C", 0, 2)


def test_row_labels_compare_ignoring_case() -> None:
    seats = seat_map(f"h: {FREE_20}", f"J: {FREE_20}")
    assert best_block(seats, 2, ["J"], ["H"]) == SeatBlock("J", 9, 2)


def test_empty_seat_map_and_oversized_n_return_none() -> None:
    assert best_block(SeatMap(rows=()), 2, [], []) is None
    assert best_block(seat_map("H: AAA"), 4, [], []) is None


@pytest.mark.parametrize("n", [0, -1])
def test_n_below_one_is_a_bug(n: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        best_block(seat_map("H: AAA"), n, [], [])


# --- T2.8: properties against the literal definition -----------------------------------------

_LABELS = [chr(ord("A") + i) for i in range(8)]


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[SeatMap, int, list[str], list[str]]:
    labels = _LABELS[: draw(st.integers(1, len(_LABELS)))]
    rows = tuple(
        (label, tuple(draw(st.lists(st.sampled_from([A, A, A, T, G]), max_size=16))))
        for label in labels
    )
    n = draw(st.integers(1, 6))
    preferred = draw(st.lists(st.sampled_from(labels), unique=True, max_size=4))
    avoid = draw(st.lists(st.sampled_from(labels), unique=True, max_size=3))
    return SeatMap(rows=rows), n, preferred, avoid


@settings(max_examples=300, deadline=None)
@given(scenarios())
def test_t2_8_result_is_valid_and_optimal(
    scenario: tuple[SeatMap, int, list[str], list[str]],
) -> None:
    seats, n, preferred, avoid = scenario
    block = best_block(seats, n, preferred, avoid)

    assert block == oracle(seats, n, preferred, avoid)
    if block is not None:
        cells = dict(seats.rows)[block.row]
        assert block.n == n
        assert block.start_col >= 0
        assert block.start_col + n <= len(cells)
        assert all(cell is A for cell in cells[block.start_col : block.start_col + n])
        assert block.row not in avoid


# --- T2.9: performance ---------------------------------------------------------------------


def test_t2_9_large_grid_is_fast() -> None:
    rng = random.Random(2026)
    seats = SeatMap(
        rows=tuple(
            (f"R{i}", tuple(rng.choice([A, A, A, T, G]) for _ in range(40))) for i in range(30)
        )
    )
    preferred = ["R12", "R13", "R14"]
    best_block(seats, 4, preferred, ["R0"])  # warm-up

    runs = 100
    started = time.perf_counter()
    for _ in range(runs):
        best_block(seats, 4, preferred, ["R0"])
    mean_ms = (time.perf_counter() - started) / runs * 1000
    assert mean_ms < 5, f"mean {mean_ms:.2f} ms"
