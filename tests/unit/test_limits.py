"""The hard limits from SPEC §2 (C4, C5) live in one module, and this test pins their values.

Changing any of these is a change to §2, which needs the user's agreement first (SPEC §0 rule 4).
If this test fails, revert the constant instead of editing the test.
"""

from showwatch import limits


def test_limits_match_spec_section_2() -> None:
    assert limits.MIN_TARGET_INTERVAL_S == 60  # C4: per-target floor
    assert limits.GLOBAL_MIN_SPACING_S == 20  # C4: at most 1 request every 20 s overall
    assert limits.MAX_BACKOFF_S == 60 * 60  # T4.7: backoff cap
    assert limits.MAX_TARGETS == 3  # C5
    assert limits.MAX_SEATS_PER_TARGET == 10  # C5
    assert limits.MAX_JITTER_PCT == 30  # §8
