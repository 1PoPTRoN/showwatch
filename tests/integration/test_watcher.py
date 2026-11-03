"""Phase 4: watcher and poll loop with a scripted adapter (SPEC §10 Phase 4, tests T4.1-T4.12).

Runs against a real store on SQLite and the virtual clock: hours of polling take milliseconds.
"""

import asyncio
import itertools
import logging
import random
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from showwatch.config import Polling
from showwatch.errors import Blocked, RateLimited, TransientError
from showwatch.models import Change, ChangeKind, EventKind
from showwatch.ratelimit import PollGate
from showwatch.state import State, Trigger
from showwatch.store import Store, TransitionResult
from showwatch.watcher.watcher import PollOutcome, Watcher, WatchLoop
from tests.support.adapters import Response, StubAdapter
from tests.support.clock import FakeClock
from tests.support.notify import RecordingNotifier
from tests.support.shows import FRI, SAT, show
from tests.support.targets import make_target

IST = ZoneInfo("Asia/Kolkata")
A, B = show("a", at="19:00"), show("b", at="20:00")
STEADY = Polling.model_validate(
    {"base_interval_s": 300, "hot_interval_s": 60, "jitter_pct": 0, "hot_windows": []}
)


async def started(store: Store, target_id: str = "t1", **overrides: Any) -> str:
    await store.add_target(make_target(id=target_id, **overrides))
    await store.fire(target_id, Trigger.START)
    return target_id


def build(
    store: Store,
    clock: FakeClock,
    adapter: StubAdapter,
    notifier: RecordingNotifier,
    *,
    polling: Polling = STEADY,
    on_detected: Any = None,
) -> tuple[Watcher, WatchLoop]:
    watcher = Watcher(
        store=store,
        adapter=adapter,
        notifier=notifier,
        clock=clock,
        gate=PollGate(clock),
        polling=polling,
        tz=IST,
        on_detected=on_detected,
    )
    loop = WatchLoop(
        watcher, store=store, clock=clock, polling=polling, tz=IST, rng=random.Random(4)
    )
    return watcher, loop


def gaps(times: Sequence[datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in itertools.pairwise(times)]


async def poll_n(watcher: Watcher, target_id: str, n: int) -> list[PollOutcome]:
    return [await watcher.poll(target_id) for _ in range(n)]


def script(target_id: str, *responses: Response) -> dict[str, list[Response]]:
    return {target_id: list(responses)}


# --- T4.1 - T4.4: what counts as a change ---------------------------------------------------


async def test_t4_1_first_poll_is_only_a_baseline(store: Store, fake_clock: FakeClock) -> None:
    tid = await started(store)
    notifier = RecordingNotifier()
    watcher, _ = build(store, fake_clock, StubAdapter(fake_clock, script(tid, {})), notifier)

    assert await watcher.poll(tid) is PollOutcome.BASELINE
    assert await store.attempts(tid) == []
    assert notifier.events == []
    state = await store.watch_state(tid)
    assert state.seen is not None
    assert dict(state.seen.open_dates) == {}
    assert state.last_poll_at == fake_clock.now()


async def test_first_poll_finding_shows_already_on_sale_says_so(
    store: Store, fake_clock: FakeClock
) -> None:
    # Otherwise a target added after booking opened would wait in silence forever (P4.9).
    tid = await started(store)
    notifier = RecordingNotifier()
    watcher, _ = build(
        store, fake_clock, StubAdapter(fake_clock, script(tid, {FRI: [A]})), notifier
    )

    assert await watcher.poll(tid) is PollOutcome.BASELINE
    assert notifier.kinds() == [EventKind.ALREADY_OPEN]
    assert await store.attempts(tid) == []
    assert (await store.get_target(tid)).state is State.WATCHING


async def test_t4_2_date_opening_is_detected_once(store: Store, fake_clock: FakeClock) -> None:
    tid = await started(store)
    notifier = RecordingNotifier()
    adapter = StubAdapter(fake_clock, script(tid, {}, {FRI: [B, A]}, {FRI: [A, B]}))
    watcher, _ = build(store, fake_clock, adapter, notifier)

    outcomes = await poll_n(watcher, tid, 3)

    assert outcomes == [PollOutcome.BASELINE, PollOutcome.CHANGED, PollOutcome.UNCHANGED]
    (attempt,) = await store.attempts(tid)
    assert attempt.changes == (Change(ChangeKind.DATE_OPENED, tid, FRI, (A, B)),)
    assert attempt.outcome == "notified"  # notify-only: alert sent, back to watching
    (event,) = notifier.events
    assert (event.kind, event.attempt_id, event.changes) == (
        EventKind.DETECTED,
        attempt.attempt_id,
        attempt.changes,
    )
    assert (await store.get_target(tid)).state is State.WATCHING


async def test_t4_3_new_show_on_an_open_date(store: Store, fake_clock: FakeClock) -> None:
    tid = await started(store)
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {FRI: [A]}, {FRI: [A, B]})),
        RecordingNotifier(),
    )
    assert await poll_n(watcher, tid, 2) == [PollOutcome.BASELINE, PollOutcome.CHANGED]
    (attempt,) = await store.attempts(tid)
    assert attempt.changes == (Change(ChangeKind.SHOW_ADDED, tid, FRI, (B,)),)


async def test_t4_4_disappearing_show_is_logged_not_detected(
    store: Store, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    tid = await started(store)
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {FRI: [A, B]}, {FRI: [A]})),
        RecordingNotifier(),
    )
    with caplog.at_level(logging.INFO, logger="showwatch.watcher"):
        assert await poll_n(watcher, tid, 2) == [PollOutcome.BASELINE, PollOutcome.UNCHANGED]
    assert await store.attempts(tid) == []
    assert any(
        "no longer listed" in r.getMessage() and "b" in r.getMessage() for r in caplog.records
    )


async def test_a_show_that_flickers_away_and_back_is_not_news(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {FRI: [A, B]}, {FRI: [A]}, {FRI: [A, B]})),
        RecordingNotifier(),
    )
    await poll_n(watcher, tid, 3)
    assert await store.attempts(tid) == []  # P4.2


async def test_shows_outside_the_targets_rules_are_ignored(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    elsewhere = show("z", venue="Cinema Z")
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {}, {FRI: [elsewhere]})),
        RecordingNotifier(),
    )
    assert await poll_n(watcher, tid, 2) == [PollOutcome.BASELINE, PollOutcome.UNCHANGED]
    assert await store.attempts(tid) == []


# --- T4.12 and detection plumbing -----------------------------------------------------------


async def test_t4_12_one_change_gives_one_attempt_and_one_detection(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    notifier = RecordingNotifier()
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {}, {FRI: [A]}, {FRI: [A]})),
        notifier,
    )
    await poll_n(watcher, tid, 3)

    assert len(await store.attempts(tid)) == 1
    assert [r.to_state for r in await store.audit(tid)].count(State.DETECTED) == 1
    assert notifier.kinds().count(EventKind.DETECTED) == 1


async def test_several_dates_opening_in_one_poll_share_one_attempt(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store, dates=["2026-12-18", "2026-12-19"])
    sat = show("s", on=SAT)
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {}, {FRI: [A], SAT: [sat]})),
        RecordingNotifier(),
    )
    await poll_n(watcher, tid, 2)
    (attempt,) = await store.attempts(tid)
    assert [(c.kind, c.on_date) for c in attempt.changes] == [
        (ChangeKind.DATE_OPENED, FRI),
        (ChangeKind.DATE_OPENED, SAT),
    ]


async def test_a_failing_notifier_never_stops_the_pipeline(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {}, {FRI: [A]})),
        RecordingNotifier(fail=True),
    )
    assert await poll_n(watcher, tid, 2) == [PollOutcome.BASELINE, PollOutcome.CHANGED]
    assert (await store.get_target(tid)).state is State.WATCHING
    assert len(await store.attempts(tid)) == 1


@pytest.mark.parametrize("mode", ["dry-run", "assist"])
async def test_selection_modes_hand_the_detection_to_the_hook(
    mode: str, store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store, mode=mode)
    handed: list[TransitionResult] = []

    async def on_detected(result: TransitionResult) -> None:
        handed.append(result)

    notifier = RecordingNotifier()
    watcher, _ = build(
        store,
        fake_clock,
        StubAdapter(fake_clock, script(tid, {}, {FRI: [A]})),
        notifier,
        on_detected=on_detected,
    )
    await poll_n(watcher, tid, 2)
    (attempt,) = await store.attempts(tid)
    assert [r.attempt_id for r in handed] == [attempt.attempt_id]
    assert notifier.kinds() == [EventKind.DETECTED]
    assert (await store.get_target(tid)).state is State.DETECTED  # the executor takes it from here


async def test_changes_seen_while_the_target_is_paused_are_not_lost(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    calls = 0

    async def pause_during_second_poll(_: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            await store.fire(tid, Trigger.USER_PAUSE)

    adapter = StubAdapter(
        fake_clock, script(tid, {}, {FRI: [A]}, {FRI: [A]}), on_call=pause_during_second_poll
    )
    watcher, _ = build(store, fake_clock, adapter, RecordingNotifier())
    await watcher.poll(tid)
    await watcher.poll(tid)  # paused mid-poll: the opening must not be absorbed into the baseline
    assert await store.attempts(tid) == []

    await store.fire(tid, Trigger.USER_RESUME)
    assert await watcher.poll(tid) is PollOutcome.CHANGED


async def test_poll_results_are_persisted(store: Store, fake_clock: FakeClock) -> None:
    tid = await started(store)
    watcher, _ = build(
        store, fake_clock, StubAdapter(fake_clock, script(tid, {FRI: [A]})), RecordingNotifier()
    )
    await watcher.poll(tid)
    state = await store.watch_state(tid)
    assert state.snapshot is not None
    assert dict(state.snapshot.open_dates) == {FRI: (A,)}
    assert state.last_success_at == fake_clock.now()


# --- T4.7 - T4.11: the loop --------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["captcha", "queue", "forbidden"])
async def test_t4_8_block_pauses_notifies_once_and_stops_polling(
    reason: str, store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    adapter = StubAdapter(fake_clock, script(tid, {}, Blocked(reason), {}))
    notifier = RecordingNotifier()
    _, loop = build(store, fake_clock, adapter, notifier)

    await loop.run(until=fake_clock.now() + timedelta(hours=2))

    assert len(adapter.calls) == 2
    assert (await store.get_target(tid)).state is State.PAUSED
    (event,) = notifier.events
    assert (event.kind, event.reason) == (EventKind.BLOCKED, reason)

    await store.fire(tid, Trigger.USER_RESUME)
    await loop.run(until=fake_clock.now() + timedelta(minutes=10))
    assert len(adapter.calls) > 2  # polling resumes only after the user resumes


async def test_t4_9_transient_errors_back_off_and_notify_once(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    adapter = StubAdapter(fake_clock, script(tid, {}, *[TransientError("503")] * 5, {}))
    notifier = RecordingNotifier()
    _, loop = build(store, fake_clock, adapter, notifier)

    assert await loop.run(max_polls=8) == 8  # the loop survives

    starts = adapter.starts(tid)
    assert gaps(starts) == [300, 600, 1200, 2400, 3600, 3600, 3600]
    (event,) = notifier.events
    assert event.kind is EventKind.POLL_FAILING
    assert event.at == starts[5]  # sent at the 5th consecutive failure


async def test_a_long_failure_streak_still_notifies_only_once(
    store: Store, fake_clock: FakeClock
) -> None:
    tid = await started(store)
    adapter = StubAdapter(fake_clock, script(tid, {}, *[TransientError("503")] * 7, {}))
    notifier = RecordingNotifier()
    _, loop = build(store, fake_clock, adapter, notifier)
    await loop.run(max_polls=9)
    assert notifier.kinds() == [EventKind.POLL_FAILING]


async def test_an_unexpected_adapter_error_does_not_kill_the_loop(
    store: Store, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    tid = await started(store)
    adapter = StubAdapter(fake_clock, script(tid, {}, ValueError("adapter bug"), {}))
    _, loop = build(store, fake_clock, adapter, RecordingNotifier())
    with caplog.at_level(logging.ERROR, logger="showwatch.watcher"):
        assert await loop.run(max_polls=4) == 4
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_t4_7_rate_limiting_backs_off_every_target(
    store: Store, fake_clock: FakeClock
) -> None:
    t1, t2 = await started(store, "t1"), await started(store, "t2")
    adapter = StubAdapter(
        fake_clock,
        {t1: [{}, RateLimited(), RateLimited(), RateLimited(), {}], t2: [{}]},
    )
    _, loop = build(store, fake_clock, adapter, RecordingNotifier())
    start = fake_clock.now()

    await loop.run(until=start + timedelta(hours=3))

    t1_starts, t2_starts = adapter.starts(t1), adapter.starts(t2)
    assert gaps(t1_starts)[:4] == [300, 600, 1200, 2400]  # doubles on every 429
    first_429 = t1_starts[1]
    assert not [s for s in t2_starts if first_429 < s < first_429 + timedelta(seconds=600)]
    assert gaps(t1_starts)[-1] == 300  # back to normal after 3 consecutive successes


async def test_t4_10_three_targets_respect_global_and_per_target_spacing(
    store: Store, fake_clock: FakeClock
) -> None:
    ids = [await started(store, f"t{i}") for i in range(3)]
    adapter = StubAdapter(fake_clock, {i: [{}] for i in ids})
    busy = Polling.model_validate({"base_interval_s": 60, "hot_interval_s": 60, "jitter_pct": 20})
    _, loop = build(store, fake_clock, adapter, RecordingNotifier(), polling=busy)

    await loop.run(until=fake_clock.now() + timedelta(hours=1))

    assert min(gaps(adapter.starts())) >= 20
    for tid in ids:
        assert len(adapter.starts(tid)) >= 20
        assert min(gaps(adapter.starts(tid))) >= 60


async def test_t4_11_requests_never_overlap(store: Store, fake_clock: FakeClock) -> None:
    ids = [await started(store, f"t{i}") for i in range(3)]
    adapter = StubAdapter(fake_clock, {i: [{}] for i in ids}, latency_s=5)
    watcher, loop = build(store, fake_clock, adapter, RecordingNotifier())

    await asyncio.gather(*(watcher.poll(i) for i in ids))  # three callers at once
    await loop.run(until=fake_clock.now() + timedelta(minutes=30))

    assert adapter.max_in_flight == 1
    calls = sorted(adapter.calls, key=lambda c: c.started)
    for earlier, later in itertools.pairwise(calls):
        assert earlier.ended is not None
        assert later.started >= earlier.ended


async def test_only_watching_targets_are_polled(store: Store, fake_clock: FakeClock) -> None:
    watching = await started(store, "watching")
    await store.add_target(make_target(id="idle"))
    paused = await started(store, "paused")
    await store.fire(paused, Trigger.USER_PAUSE)
    adapter = StubAdapter(fake_clock, {i: [{}] for i in (watching, "idle", paused)})
    _, loop = build(store, fake_clock, adapter, RecordingNotifier())

    await loop.run(until=fake_clock.now() + timedelta(minutes=20))
    assert {c.target_id for c in adapter.calls} == {watching}
