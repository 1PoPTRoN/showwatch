"""The heartbeat (T5.7): every target with its state and when it was last checked."""

from zoneinfo import ZoneInfo

from showwatch.clock import Clock
from showwatch.config import DEFAULT_TIMEZONE
from showwatch.models import Event, EventKind, TargetStatus
from showwatch.store import Store


async def heartbeat_event(
    store: Store, clock: Clock, *, tz: ZoneInfo | None = None, manual: bool = False
) -> Event:
    """Dedupes per local day; a manual one (`showwatch heartbeat`) always sends."""
    zone = tz or ZoneInfo(DEFAULT_TIMEZONE)
    now = clock.now()
    statuses = []
    for record in await store.list_targets():
        watch = await store.watch_state(record.target.id)
        statuses.append(
            TargetStatus(
                record.target.id, record.target.movie, record.state.value, watch.last_poll_at
            )
        )
    day = now.astimezone(zone).date().isoformat()
    event_id = f"heartbeat:manual:{now.isoformat()}" if manual else f"heartbeat:{day}"
    return Event(event_id=event_id, kind=EventKind.HEARTBEAT, at=now, statuses=tuple(statuses))
