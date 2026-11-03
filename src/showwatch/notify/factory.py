"""Build the configured notifier, wrapped for dedup. Startup errors are clear and secret-free."""

from collections.abc import Mapping
from typing import Literal, TextIO
from zoneinfo import ZoneInfo

import httpx

from showwatch.clock import Clock
from showwatch.notify.base import Notifier
from showwatch.notify.console import ConsoleNotifier
from showwatch.notify.dedup import DedupNotifier
from showwatch.notify.telegram import TelegramConfig, TelegramNotifier
from showwatch.store import Store


def build_notifier(
    kind: Literal["telegram", "console"],
    environ: Mapping[str, str],
    *,
    store: Store,
    client: httpx.AsyncClient,
    clock: Clock,
    tz: ZoneInfo,
    stream: TextIO | None = None,
) -> Notifier:
    """Raises NotifierConfigError if `kind` is telegram and its env vars are missing or invalid."""
    inner: Notifier
    if kind == "telegram":
        inner = TelegramNotifier(
            TelegramConfig.from_env(environ), client=client, clock=clock, tz=tz
        )
    else:
        inner = ConsoleNotifier(tz=tz, stream=stream)
    return DedupNotifier(inner, store=store)
