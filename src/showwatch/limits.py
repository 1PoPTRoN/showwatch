"""Hard limits from SPEC §2. Configuration can never raise or lower these.

Config validation and the rate limiter both import them from here, so each number has one source.
tests/unit/test_limits.py pins the values: changing one is a change to §2 and needs the user's
agreement first.
"""

MIN_TARGET_INTERVAL_S = 60  # C4: one target is never polled more often than this
GLOBAL_MIN_SPACING_S = 20  # C4: at most one request every 20 s across all targets
MAX_BACKOFF_S = 60 * 60  # T4.7: exponential backoff is capped at 60 minutes
MAX_TARGETS = 3  # C5
MAX_SEATS_PER_TARGET = 10  # C5
MAX_JITTER_PCT = 30  # SPEC §8: jitter_pct is 0..30
