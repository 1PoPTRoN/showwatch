"""Typed exceptions (SPEC §5: errors are typed, never bare).

Every §7 error is defined here.
"""

from collections.abc import Sequence


class ConfigError(Exception):
    """The configuration is invalid. `problems` lists every issue found, one line each.

    Messages name keys and locations but never echo input values, so a secret pasted into the
    wrong place cannot leak through an error message (C7).
    """

    def __init__(self, problems: Sequence[str], source: str | None = None) -> None:
        self.problems = list(problems)
        self.source = source
        header = f"Invalid config ({source})" if source else "Invalid config"
        super().__init__(header + ":\n" + "\n".join(f"  - {p}" for p in self.problems))


class IllegalTransition(Exception):
    """A trigger the state machine does not allow from the current state or mode (SPEC §3)."""


class StoreError(Exception):
    """The store cannot do what was asked."""


class UnknownTarget(StoreError):
    """No target with this id exists."""


class UnknownAttempt(StoreError):
    """No attempt with this id exists."""


class DuplicateTarget(StoreError):
    """A target with this id already exists."""


class TargetLimitReached(StoreError):
    """Adding the target would exceed the personal-scale target limit (C5)."""


class AttemptConflict(StoreError):
    """The database refused a second open attempt for one target."""


class RateLimited(Exception):
    """The site asked us to slow down (HTTP 429). Every target backs off (C4)."""


class Blocked(Exception):
    """A CAPTCHA, a virtual queue or a block page (403). The target pauses; no evasion (C3)."""

    REASONS = frozenset({"captcha", "queue", "forbidden"})

    def __init__(self, reason: str) -> None:
        if reason not in self.REASONS:
            raise ValueError(
                f"Blocked reason must be one of {sorted(self.REASONS)}, got {reason!r}"
            )
        super().__init__(reason)
        self.reason = reason


class TransientError(Exception):
    """A temporary failure (5xx, timeout, network). Retried later with backoff."""


class NotifierConfigError(Exception):
    """The configured notifier cannot start (for example, Telegram env vars are missing)."""


class NotificationFailed(Exception):
    """A message could not be delivered. Its text never holds secrets or token-bearing URLs."""


class LocatorNotFound(Exception):
    """An element a booking step needs was not found: the site has probably changed (T7.9)."""

    def __init__(self, step: str) -> None:
        super().__init__(f"no element found for step {step!r}")
        self.step = step


class PaymentBoundaryError(Exception):
    """A click on the payment page, or on a forbidden locator, was refused (C1)."""


class AlreadyRunning(Exception):
    """Another ShowWatch process holds the lock (only one `run`, `login` or `canary` at a time)."""
