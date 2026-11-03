"""Phase 3: the SQLite store and the state machine together (SPEC §3, tests T3.1-T3.8).

Targets reach a starting state only through legal transitions (`path_to` searches the state
machine), so no test can forge a state the machine would never produce.
"""

import asyncio
import dataclasses
import itertools
import random
import sqlite3
from collections import deque
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from showwatch import store as store_module
from showwatch.config import Mode
from showwatch.errors import (
    DuplicateTarget,
    IllegalTransition,
    StoreError,
    TargetLimitReached,
    UnknownTarget,
)
from showwatch.models import Change, ChangeKind, Show
from showwatch.state import GUARDS, TRANSITIONS, State, StateMachine, Trigger
from showwatch.store import Store, TransitionResult
from tests.support.clock import FakeClock
from tests.support.targets import make_target

SHOW = Show(
    "s1", "Cinema A", "IMAX 2D", "hindi", datetime(2026, 12, 18, 13, 0, tzinfo=UTC), 450, "u"
)
CHANGE = Change(ChangeKind.DATE_OPENED, "movie-fri", date(2026, 12, 18), (SHOW,))


def change_for(target_id: str) -> Change:
    return dataclasses.replace(CHANGE, target_id=target_id)


NEEDS_ATTEMPT = {
    State.DETECTED,
    State.SELECTING,
    State.HELD,
    State.AWAITING_PAYMENT,
    State.EXPIRED,
    State.RELEASED,
    State.FAILED,
}
NO_ATTEMPT = {State.IDLE, State.WATCHING, State.BOOKED}


def path_to(goal: State, mode: Mode) -> list[Trigger]:
    """The shortest legal trigger sequence from IDLE to `goal` for a target in `mode`."""
    queue: deque[tuple[State, list[Trigger]]] = deque([(State.IDLE, [])])
    seen = {State.IDLE}
    while queue:
        state, path = queue.popleft()
        if state is goal:
            return path
        for trigger in Trigger:
            if trigger is Trigger.RECOVER:
                continue  # only recovery uses it
            try:
                nxt = StateMachine.next_state(state, trigger, mode)
            except IllegalTransition:
                continue
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, [*path, trigger]))
    raise AssertionError(f"{goal} is unreachable in {mode}")


async def step(
    store: Store, target_id: str, trigger: Trigger, clock: FakeClock, hold_s: float | None = 600
) -> TransitionResult:
    """Fire `trigger` with the extra data the store requires for it."""
    if trigger is Trigger.CHANGE_DETECTED:
        return await store.fire(target_id, trigger, changes=[change_for(target_id)])
    if trigger is Trigger.SUMMARY_REACHED and hold_s is not None:
        return await store.fire(
            target_id, trigger, expires_at=clock.now() + timedelta(seconds=hold_s)
        )
    return await store.fire(target_id, trigger)


async def new_target(
    store: Store, clock: FakeClock, state: State, mode: Mode = Mode.ASSIST, target_id: str = "t1"
) -> str:
    await store.add_target(make_target(id=target_id, mode=mode.value))
    for trigger in path_to(state, mode):
        await step(store, target_id, trigger, clock)
    assert (await store.get_target(target_id)).state is state
    return target_id


def mode_for(key: tuple[State, Trigger]) -> Mode:
    allowed = GUARDS.get(key, frozenset(Mode))
    return Mode.ASSIST if Mode.ASSIST in allowed else sorted(allowed)[0]


# --- T3.1 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "expected"), sorted(TRANSITIONS.items()))
async def test_t3_1_every_legal_transition_writes_one_audit_row(
    key: tuple[State, Trigger], expected: State, store: Store, fake_clock: FakeClock
) -> None:
    state, trigger = key
    target_id = await new_target(store, fake_clock, state, mode_for(key))
    before = await store.audit(target_id)
    fake_clock.advance(7)

    result = await step(store, target_id, trigger, fake_clock)

    after = await store.audit(target_id)
    assert len(after) == len(before) + 1
    row = after[-1]
    assert (row.from_state, row.to_state, row.trigger) == (state, expected, trigger)
    assert row.at == fake_clock.now()
    assert row.attempt_id == result.attempt_id
    assert (result.from_state, result.to_state) == (state, expected)
    assert (await store.get_target(target_id)).state is expected


async def test_attempt_opens_on_detection_and_closes_on_return_to_watching(
    store: Store, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, State.WATCHING, Mode.NOTIFY_ONLY)
    detected = await step(store, target_id, Trigger.CHANGE_DETECTED, fake_clock)
    attempt = await store.open_attempt(target_id)
    assert attempt is not None
    assert attempt.attempt_id == detected.attempt_id
    assert attempt.changes == (change_for(target_id),)
    assert attempt.opened_at == fake_clock.now()

    fake_clock.advance(3)
    notified = await step(store, target_id, Trigger.NOTIFIED, fake_clock)
    assert notified.attempt_id == attempt.attempt_id
    assert await store.open_attempt(target_id) is None
    closed = await store.get_attempt(attempt.attempt_id)
    assert closed.closed_at == fake_clock.now()
    assert closed.outcome == "notified"


async def test_target_only_transitions_have_no_attempt(store: Store, fake_clock: FakeClock) -> None:
    target_id = await new_target(store, fake_clock, State.PAUSED, Mode.NOTIFY_ONLY)
    resumed = await step(store, target_id, Trigger.USER_RESUME, fake_clock)
    assert resumed.attempt_id is None
    assert all(row.attempt_id is None for row in await store.audit(target_id))


async def test_blocked_mid_selection_keeps_the_attempt_until_resume(
    store: Store, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, State.SELECTING)
    await store.fire(target_id, Trigger.BLOCKED, reason="captcha")
    attempt = await store.open_attempt(target_id)
    assert attempt is not None  # PAUSED mid-flow: no new attempt can start until resume
    await store.fire(target_id, Trigger.USER_RESUME)
    assert (await store.get_attempt(attempt.attempt_id)).outcome == "blocked"
    assert await store.open_attempt(target_id) is None


async def test_hold_expiry_is_stored_on_the_attempt(store: Store, fake_clock: FakeClock) -> None:
    target_id = await new_target(store, fake_clock, State.HELD)
    expires = fake_clock.now() + timedelta(minutes=8)
    await store.fire(target_id, Trigger.SUMMARY_REACHED, expires_at=expires)
    attempt = await store.open_attempt(target_id)
    assert attempt is not None
    assert attempt.expires_at == expires


# --- T3.2 ------------------------------------------------------------------------------------


async def _snapshot(store: Store, target_id: str) -> tuple[object, ...]:
    return (
        await store.get_target(target_id),
        await store.audit(target_id),
        await store.attempts(target_id),
    )


async def test_t3_2_illegal_transition_writes_nothing(store: Store, fake_clock: FakeClock) -> None:
    target_id = await new_target(store, fake_clock, State.WATCHING)
    before = await _snapshot(store, target_id)
    with pytest.raises(IllegalTransition):
        await store.fire(target_id, Trigger.USER_DONE)
    assert await _snapshot(store, target_id) == before


async def test_t3_2_a_failure_partway_through_a_transition_writes_nothing(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    # Atomicity: make the *last* write of a transition (the audit row) fail. The earlier writes in
    # the same transition (a new attempt, the new state) must be rolled back with it.
    target_id = await new_target(store, fake_clock, State.WATCHING)
    before = await _snapshot(store, target_id)
    raw = sqlite3.connect(tmp_path / "showwatch.db")
    try:
        raw.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    finally:
        raw.close()

    with pytest.raises(StoreError, match="injected failure"):
        await store.fire(target_id, Trigger.CHANGE_DETECTED, changes=[change_for(target_id)])
    assert await _snapshot(store, target_id) == before


async def test_corrupt_database_file_is_a_store_error(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not an SQLite database" * 100)
    with pytest.raises(StoreError, match=r"corrupt\.db"):
        await Store.open(db, clock=fake_clock)


async def test_mode_guard_violation_writes_nothing(store: Store, fake_clock: FakeClock) -> None:
    # A notify-only target must never start seat selection, not even by mistake.
    target_id = await new_target(store, fake_clock, State.DETECTED, Mode.NOTIFY_ONLY)
    before = await _snapshot(store, target_id)
    with pytest.raises(IllegalTransition, match="notify-only"):
        await store.fire(target_id, Trigger.BEGIN_SELECT)
    assert await _snapshot(store, target_id) == before


# --- T3.3 ------------------------------------------------------------------------------------


def _outcomes(results: Sequence[TransitionResult | BaseException]) -> tuple[int, int]:
    wins = sum(isinstance(r, TransitionResult) for r in results)
    losses = sum(isinstance(r, IllegalTransition) for r in results)
    return wins, losses


async def test_t3_3_concurrent_detection_creates_one_attempt(
    store: Store, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, State.WATCHING)
    results = await asyncio.gather(
        store.fire(target_id, Trigger.CHANGE_DETECTED, changes=[change_for(target_id)]),
        store.fire(target_id, Trigger.CHANGE_DETECTED, changes=[change_for(target_id)]),
        return_exceptions=True,
    )
    assert _outcomes(results) == (1, 1)
    assert len(await store.attempts(target_id)) == 1


async def test_t3_3_two_processes_racing_create_one_attempt(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    # Two Store instances = two connections, as with `showwatch run` and a CLI command.
    db = tmp_path / "shared.db"
    async with (
        await Store.open(db, clock=fake_clock) as first,
        await Store.open(db, clock=fake_clock) as second,
    ):
        target_id = await new_target(first, fake_clock, State.WATCHING)
        results = await asyncio.gather(
            *(
                s.fire(target_id, Trigger.CHANGE_DETECTED, changes=[change_for(target_id)])
                for s in (first, second, first, second)
            ),
            return_exceptions=True,
        )
        assert _outcomes(results) == (1, 3)
        assert len(await second.attempts(target_id)) == 1


async def test_t3_3_database_itself_refuses_a_second_open_attempt(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    # The unique partial index is the last line of defence, even against buggy code.
    target_id = await new_target(store, fake_clock, State.DETECTED)
    raw = sqlite3.connect(tmp_path / "showwatch.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            raw.execute(
                "INSERT INTO attempts (id, target_id, opened_at, changes_json) "
                "VALUES ('forged', ?, 'x', '[]')",
                (target_id,),
            )
    finally:
        raw.close()


# --- T3.4 / T3.5: recovery on startup -------------------------------------------------------


async def _restart(store: Store, tmp_path: Path, clock: FakeClock) -> Store:
    await store.close()
    return await Store.open(tmp_path / "showwatch.db", clock=clock)


async def test_t3_4_selecting_is_recovered_to_watching(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, State.SELECTING)
    attempt = await store.open_attempt(target_id)
    assert attempt is not None
    restarted = await _restart(store, tmp_path, fake_clock)
    try:
        report = await restarted.recover()

        assert (await restarted.get_target(target_id)).state is State.WATCHING
        assert [(t.from_state, t.trigger) for t in report.transitions] == [
            (State.SELECTING, Trigger.RECOVER)
        ]
        assert [a.attempt_id for a in report.interrupted] == [attempt.attempt_id]
        assert (await restarted.get_attempt(attempt.attempt_id)).outcome == "recover"
        assert await restarted.open_attempt(target_id) is None
        again = await restarted.recover()  # idempotent
        assert again.transitions == ()
    finally:
        await restarted.close()


async def test_detected_is_recovered_and_its_change_kept_for_renotifying(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    # A crash between detecting an opening and alerting the user must not lose the alert.
    target_id = await new_target(store, fake_clock, State.DETECTED, Mode.NOTIFY_ONLY)
    restarted = await _restart(store, tmp_path, fake_clock)
    try:
        report = await restarted.recover()
        assert (await restarted.get_target(target_id)).state is State.WATCHING
        assert [a.changes for a in report.interrupted] == [(change_for(target_id),)]
    finally:
        await restarted.close()


async def test_t3_5_expired_hold_goes_expired_then_watching(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, State.AWAITING_PAYMENT)  # holds for 600 s
    fake_clock.advance(601)
    restarted = await _restart(store, tmp_path, fake_clock)
    try:
        report = await restarted.recover()
        assert [(t.from_state, t.to_state) for t in report.transitions] == [
            (State.AWAITING_PAYMENT, State.EXPIRED),
            (State.EXPIRED, State.WATCHING),
        ]
        assert report.holds_still_running == ()
        assert (await restarted.get_target(target_id)).state is State.WATCHING
        assert [row.to_state for row in (await restarted.audit(target_id))[-2:]] == [
            State.EXPIRED,
            State.WATCHING,
        ]
    finally:
        await restarted.close()


async def test_t3_5_running_hold_stays_and_is_reported_for_the_countdown(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, State.AWAITING_PAYMENT)
    fake_clock.advance(120)
    restarted = await _restart(store, tmp_path, fake_clock)
    try:
        report = await restarted.recover()
        assert report.transitions == ()
        (hold,) = report.holds_still_running
        assert hold.target_id == target_id
        assert hold.expires_at is not None
        assert hold.expires_at - fake_clock.now() == timedelta(seconds=480)
        assert (await restarted.get_target(target_id)).state is State.AWAITING_PAYMENT
    finally:
        await restarted.close()


async def test_held_with_unknown_expiry_is_treated_as_expired(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    # A crash between holding seats and reading the countdown: the hold cannot be tracked.
    target_id = await new_target(store, fake_clock, State.HELD)
    restarted = await _restart(store, tmp_path, fake_clock)
    try:
        report = await restarted.recover()
        assert report.transitions[0].reason == "hold expiry unknown after restart"
        assert (await restarted.get_target(target_id)).state is State.WATCHING
    finally:
        await restarted.close()


@pytest.mark.parametrize("state", [State.EXPIRED, State.RELEASED])
async def test_crash_before_rewatch_is_completed(
    state: State, store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, state)
    restarted = await _restart(store, tmp_path, fake_clock)
    try:
        await restarted.recover()
        assert (await restarted.get_target(target_id)).state is State.WATCHING
    finally:
        await restarted.close()


# --- T3.6 ------------------------------------------------------------------------------------


async def test_t3_6_nothing_leaves_booked(store: Store, fake_clock: FakeClock) -> None:
    target_id = await new_target(store, fake_clock, State.BOOKED)
    (attempt,) = await store.attempts(target_id)
    assert attempt.outcome == "booked"
    before = await _snapshot(store, target_id)
    for trigger in Trigger:
        with pytest.raises(IllegalTransition):
            await store.fire(
                target_id,
                trigger,
                changes=[change_for(target_id)] if trigger is Trigger.CHANGE_DETECTED else None,
            )
    assert await _snapshot(store, target_id) == before


# --- T3.7 / T3.8 -----------------------------------------------------------------------------


async def test_t3_7_data_survives_close_and_reopen_in_wal_mode(
    store: Store, tmp_path: Path, fake_clock: FakeClock
) -> None:
    target_id = await new_target(store, fake_clock, State.AWAITING_PAYMENT)
    before = await _snapshot(store, target_id)
    assert await store.journal_mode() == "wal"
    reopened = await _restart(store, tmp_path, fake_clock)
    try:
        assert await _snapshot(reopened, target_id) == before
        assert await reopened.journal_mode() == "wal"
    finally:
        await reopened.close()


async def test_t3_8_migrations_are_idempotent(tmp_path: Path, fake_clock: FakeClock) -> None:
    db = tmp_path / "m.db"
    for _ in range(3):
        async with await Store.open(db, clock=fake_clock) as store:
            assert await store.schema_version() == len(store_module.MIGRATIONS)
    raw = sqlite3.connect(db, autocommit=True)
    try:
        assert store_module.migrate(raw) == len(store_module.MIGRATIONS)
        assert store_module.migrate(raw) == len(store_module.MIGRATIONS)
    finally:
        raw.close()


async def test_failed_migration_leaves_the_previous_version(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "m.db"
    async with await Store.open(db, clock=fake_clock):
        pass
    broken = (*store_module.MIGRATIONS, ("CREATE TABLE half_done (x)", "THIS IS NOT SQL"))
    monkeypatch.setattr(store_module, "MIGRATIONS", broken)
    with pytest.raises(StoreError, match=f"migration {len(broken)}"):
        await Store.open(db, clock=fake_clock)

    raw = sqlite3.connect(db)
    try:
        assert raw.execute("PRAGMA user_version").fetchone() == (len(broken) - 1,)
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "half_done" not in tables
    finally:
        raw.close()


async def test_migration_2_converts_single_changes_into_lists(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build a schema-1 database by hand, holding an attempt in the old single-change format.
    db = tmp_path / "v1.db"
    raw = sqlite3.connect(db, autocommit=True)
    monkeypatch.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:1])
    assert store_module.migrate(raw) == 1
    monkeypatch.undo()
    target = make_target(id="old")
    old_change = change_for("old")
    raw.execute(
        "INSERT INTO targets VALUES ('old', ?, 'DETECTED', '2026-11-03T03:30:00+00:00', "
        "'2026-11-03T03:30:00+00:00')",
        (target.model_dump_json(),),
    )
    raw.execute(
        "INSERT INTO attempts (id, target_id, opened_at, change_json) "
        "VALUES ('old-1', 'old', '2026-11-03T03:30:00+00:00', ?)",
        (TypeAdapter(Change).dump_json(old_change).decode(),),
    )
    raw.close()

    async with await Store.open(db, clock=fake_clock) as store:
        assert await store.schema_version() == len(store_module.MIGRATIONS)
        assert (await store.get_attempt("old-1")).changes == (old_change,)


async def test_changes_must_belong_to_the_target(store: Store, fake_clock: FakeClock) -> None:
    target_id = await new_target(store, fake_clock, State.WATCHING)
    with pytest.raises(ValueError, match="not 't1'"):
        await store.fire(target_id, Trigger.CHANGE_DETECTED, changes=[change_for("other")])
    with pytest.raises(ValueError, match="change"):
        await store.fire(target_id, Trigger.CHANGE_DETECTED, changes=[])


async def test_database_from_a_newer_version_is_refused(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    db = tmp_path / "future.db"
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(StoreError, match="newer"):
        await Store.open(db, clock=fake_clock)


# --- targets ---------------------------------------------------------------------------------


async def test_target_round_trips_exactly(store: Store) -> None:
    target = make_target(
        id="round-trip", mode="dry-run", preferred_rows=["h"], dates=["2020-01-01"]
    )
    await store.add_target(target)
    record = await store.get_target("round-trip")
    assert record.target == target
    assert record.state is State.IDLE


async def test_duplicate_and_unknown_targets(store: Store) -> None:
    await store.add_target(make_target(id="dup"))
    with pytest.raises(DuplicateTarget):
        await store.add_target(make_target(id="dup"))
    with pytest.raises(UnknownTarget):
        await store.get_target("nope")
    with pytest.raises(UnknownTarget):
        await store.fire("nope", Trigger.START)


async def test_at_most_three_unfinished_targets(store: Store, fake_clock: FakeClock) -> None:
    # A booked target is finished and no longer counts against the limit (C5).
    await new_target(store, fake_clock, State.BOOKED, Mode.ASSIST, target_id="finished")
    for i in range(3):
        await store.add_target(make_target(id=f"t{i}"))
    with pytest.raises(TargetLimitReached, match="3"):
        await store.add_target(make_target(id="t3"))


# --- argument checks -------------------------------------------------------------------------


async def test_detection_requires_its_change(store: Store, fake_clock: FakeClock) -> None:
    target_id = await new_target(store, fake_clock, State.WATCHING)
    with pytest.raises(ValueError, match="change"):
        await store.fire(target_id, Trigger.CHANGE_DETECTED)


async def test_expiry_only_with_a_hold_and_tz_aware(store: Store, fake_clock: FakeClock) -> None:
    target_id = await new_target(store, fake_clock, State.HELD)
    with pytest.raises(ValueError, match="expires_at"):
        await store.fire(target_id, Trigger.USER_CANCEL, expires_at=fake_clock.now())
    with pytest.raises(ValueError, match="tz-aware"):
        await store.fire(
            target_id,
            Trigger.SUMMARY_REACHED,
            expires_at=datetime(2026, 11, 3, 9, 30),  # noqa: DTZ001 (deliberately naive)
        )


async def test_closed_store_refuses_use(tmp_path: Path, fake_clock: FakeClock) -> None:
    db = await Store.open(tmp_path / "c.db", clock=fake_clock)
    await db.close()
    with pytest.raises(StoreError, match="closed"):
        await db.list_targets()


# --- invariants under a random walk ---------------------------------------------------------


async def test_invariants_hold_under_random_triggers(store: Store, fake_clock: FakeClock) -> None:
    rng = random.Random(3)
    targets = {"notify": Mode.NOTIFY_ONLY, "dry": Mode.DRY_RUN, "assist": Mode.ASSIST}
    for target_id, mode in targets.items():
        await store.add_target(make_target(id=target_id, mode=mode.value))

    for _ in range(400):
        target_id = rng.choice(list(targets))
        trigger = rng.choice(list(Trigger))
        fake_clock.advance(rng.choice([0, 1, 30]))
        before = await _snapshot(store, target_id)
        try:
            await step(store, target_id, trigger, fake_clock, hold_s=rng.choice([None, 300]))
        except IllegalTransition:
            assert await _snapshot(store, target_id) == before

        record = await store.get_target(target_id)
        attempts = await store.attempts(target_id)
        open_attempts = [a for a in attempts if a.closed_at is None]
        audit = await store.audit(target_id)
        assert len(open_attempts) <= 1
        if record.state in NEEDS_ATTEMPT:
            assert len(open_attempts) == 1, record.state
        if record.state in NO_ATTEMPT:
            assert open_attempts == [], record.state
        for earlier, later in itertools.pairwise(audit):
            assert later.from_state is earlier.to_state  # an unbroken chain
        assert (audit[-1].to_state if audit else State.IDLE) is record.state
