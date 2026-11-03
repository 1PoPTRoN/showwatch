"""Typed exceptions (SPEC §5: errors are typed, never bare).

The §7 errors (RateLimited, Blocked, TransientError, LocatorNotFound, PaymentBoundaryError,
IllegalTransition) are added here by the phases that raise them.
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
