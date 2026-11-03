"""Console notifier: plain-text messages on stdout (or an injected stream). Needs no secrets."""

import sys
from typing import TextIO
from zoneinfo import ZoneInfo

from showwatch.models import Event
from showwatch.notify.messages import local_clock, render


class ConsoleNotifier:
    def __init__(self, *, tz: ZoneInfo, stream: TextIO | None = None) -> None:
        self._tz = tz
        self._stream = stream

    async def send(self, event: Event) -> None:
        out = self._stream if self._stream is not None else sys.stdout
        stamp = local_clock(event.at, self._tz)
        out.write(f"[ShowWatch {stamp}] {render(event, tz=self._tz, html=False)}\n")
        out.flush()
