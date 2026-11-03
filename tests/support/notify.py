"""A notifier that records events, and can be told to fail, for pipeline tests."""

from showwatch.models import Event, EventKind


class RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[Event] = []

    async def send(self, event: Event) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("notifier is down (test)")

    def kinds(self) -> list[EventKind]:
        return [event.kind for event in self.events]
