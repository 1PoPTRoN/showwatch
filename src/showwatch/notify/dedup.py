"""The notifier the pipeline talks to: each event id is delivered at most once, and it never raises.

An event is recorded as sent only *after* the inner notifier delivered it. A failed delivery can
therefore be tried again later, and a crash between sending and recording at worst repeats one
message; an alert is never lost.
"""

import logging

from showwatch.errors import NotificationFailed
from showwatch.models import Event
from showwatch.notify.base import Notifier
from showwatch.store import Store

log = logging.getLogger(__name__)


class DedupNotifier:
    def __init__(self, inner: Notifier, *, store: Store) -> None:
        self._inner = inner
        self._store = store

    async def send(self, event: Event) -> None:
        try:
            if await self._store.notification_sent(event.event_id):
                log.debug("not sending %s again", event.event_id)
                return
            await self._inner.send(event)
            await self._store.mark_notification_sent(event)
        except Exception as exc:
            log.error(
                "could not deliver the %s alert for %s: %s",
                event.kind,
                event.target_id or "all targets",
                exc,
                exc_info=not isinstance(exc, NotificationFailed),
            )
