"""The notifier protocol (SPEC §7). Implementations arrive in Phase 5 (console, Telegram)."""

from typing import Protocol

from showwatch.models import Event


class Notifier(Protocol):
    async def send(self, event: Event) -> None: ...
