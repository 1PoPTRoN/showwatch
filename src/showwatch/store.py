"""SQLite persistence for targets, attempts and the audit trail (SPEC §3, §4).

Design choices that keep the data consistent:

* `fire()` is the only way to change a target's state. In one transaction it applies the state
  machine, opens or closes the attempt, and writes the audit row: all of it happens or none of it.
* Writes use `BEGIN IMMEDIATE` on a WAL database, so several processes (`showwatch run`, and a
  CLI command such as `showwatch done`) can share the file. Each write re-reads the current state
  under the write lock instead of trusting a cached copy.
* A unique partial index allows at most one open attempt per target, even against buggy code.
* Each Store has one connection. Every call runs in a worker thread (the event loop never blocks)
  and holds a thread lock, so a cancelled task can never leave two threads on one connection.
* `record_poll()` saves a poll result and, if it found something new, opens the attempt in the same
  transaction. A crash can neither lose a detection (baseline saved, attempt not) nor duplicate one
  (attempt saved, baseline not).
* Times come from the injected Clock and are stored as UTC ISO-8601 text.
"""

import asyncio
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Concatenate, Self, TypedDict

from pydantic import TypeAdapter

from showwatch import limits
from showwatch.clock import Clock
from showwatch.config import Target
from showwatch.errors import (
    AttemptConflict,
    DuplicateTarget,
    StoreError,
    TargetLimitReached,
    UnknownAttempt,
    UnknownTarget,
)
from showwatch.models import Attempt, Change, Event, Show, Snapshot
from showwatch.state import State, StateMachine, Trigger

BUSY_TIMEOUT_S = 10.0

# Numbered migrations: entry N is applied, atomically, to bring the schema from N-1 to N.
# Never edit an entry once released; append a new one instead.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # 1: targets, attempts, audit trail
        """CREATE TABLE targets (
            id TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) STRICT""",
        """CREATE TABLE attempts (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL REFERENCES targets(id),
            opened_at TEXT NOT NULL,
            change_json TEXT NOT NULL,
            expires_at TEXT,
            closed_at TEXT,
            outcome TEXT
        ) STRICT""",
        "CREATE UNIQUE INDEX attempts_one_open_per_target ON attempts(target_id) "
        "WHERE closed_at IS NULL",
        """CREATE TABLE audit (
            id INTEGER PRIMARY KEY,
            target_id TEXT NOT NULL REFERENCES targets(id),
            attempt_id TEXT REFERENCES attempts(id),
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            event TEXT NOT NULL,
            reason TEXT,
            at TEXT NOT NULL
        ) STRICT""",
        "CREATE INDEX audit_by_target ON audit(target_id, id)",
    ),
    (  # 2: an attempt carries every change found by its poll; per-target watch state
        "ALTER TABLE attempts RENAME COLUMN change_json TO changes_json",
        "UPDATE attempts SET changes_json = json_array(json(changes_json)) "
        "WHERE json_type(changes_json) = 'object'",
        """CREATE TABLE watch (
            target_id TEXT PRIMARY KEY REFERENCES targets(id),
            last_poll_at TEXT,
            last_success_at TEXT,
            snapshot_json TEXT,
            seen_json TEXT
        ) STRICT""",
    ),
    (  # 3: notification delivery log (each event id at most once)
        """CREATE TABLE notifications (
            event_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            target_id TEXT,
            sent_at TEXT NOT NULL
        ) STRICT""",
    ),
)


class _SnapshotRecord(TypedDict):
    # Snapshot.open_dates is a read-only mapping, which pydantic cannot serialise directly.
    target_id: str
    taken_at: datetime
    open_dates: dict[date, tuple[Show, ...]]


_CHANGES = TypeAdapter(tuple[Change, ...])
_SNAPSHOT = TypeAdapter(_SnapshotRecord)


def _snapshot_json(snapshot: Snapshot) -> str:
    record: _SnapshotRecord = {
        "target_id": snapshot.target_id,
        "taken_at": snapshot.taken_at,
        "open_dates": dict(snapshot.open_dates),
    }
    return _SNAPSHOT.dump_json(record).decode()


def _snapshot(text: str | None) -> Snapshot | None:
    if text is None:
        return None
    record = _SNAPSHOT.validate_json(text)
    return Snapshot(record["target_id"], record["taken_at"], record["open_dates"])


@dataclass(frozen=True)
class TargetRecord:
    target: Target
    state: State
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TransitionResult:
    target_id: str
    attempt_id: str | None  # None for target-only transitions (start, pause, resume, ...)
    from_state: State
    to_state: State
    trigger: Trigger
    reason: str | None
    at: datetime


@dataclass(frozen=True)
class AuditEntry:
    target_id: str
    attempt_id: str | None
    from_state: State
    to_state: State
    trigger: Trigger
    reason: str | None
    at: datetime


@dataclass(frozen=True)
class WatchState:
    """What the watcher remembers about a target between polls."""

    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    snapshot: Snapshot | None = None  # the latest raw result
    seen: Snapshot | None = None  # every relevant show ever seen: the change-detection baseline


@dataclass(frozen=True)
class Recovery:
    """What `Store.recover()` did on startup (SPEC §3 invariants)."""

    transitions: tuple[TransitionResult, ...] = ()
    # Closed by `recover`. Re-send their opening alert, or say that selection was interrupted.
    interrupted: tuple[Attempt, ...] = ()
    # Seats still held with time left. Notify the countdown again.
    holds_still_running: tuple[Attempt, ...] = ()


def _random_attempt_id(target_id: str) -> str:
    return f"{target_id}-{secrets.token_hex(3)}"


def _ts(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"datetimes must be tz-aware, got naive {value!r}")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _opt_dt(text: str | None) -> datetime | None:
    return None if text is None else _dt(text)


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the schema up to date. Safe to run repeatedly, and from several processes at once."""
    latest = len(MIGRATIONS)
    while True:
        conn.execute("BEGIN IMMEDIATE")  # re-read the version under the write lock
        try:
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current > latest:
                raise StoreError(
                    f"the database was created by a newer ShowWatch (schema {current}; this "
                    f"version knows {latest}); refusing to touch it"
                )
            if current == latest:
                conn.execute("COMMIT")
                return latest
            number = current + 1
            try:
                for statement in MIGRATIONS[number - 1]:
                    conn.execute(statement)
            except sqlite3.Error as exc:
                raise StoreError(f"migration {number} failed: {exc}") from exc
            conn.execute(f"PRAGMA user_version = {number:d}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=False, autocommit=True)
    try:
        conn.row_factory = sqlite3.Row
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise StoreError(f"could not switch the database to WAL mode (got {mode!r})")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _check_changes(
    target_id: str, trigger: Trigger, changes: Sequence[Change] | None
) -> tuple[Change, ...]:
    found = tuple(changes or ())
    if (trigger is Trigger.CHANGE_DETECTED) != bool(found):
        raise ValueError("changes must be given with change_detected, and only then")
    for change in found:
        if change.target_id != target_id:
            raise ValueError(f"change is for {change.target_id!r}, not {target_id!r}")
    return found


def _outcome(previous: State, trigger: Trigger) -> str:
    if trigger is Trigger.USER_DONE:
        return "booked"
    if previous in (State.EXPIRED, State.RELEASED, State.FAILED):
        return previous.value.lower()
    if previous is State.PAUSED:
        return "blocked"
    return trigger.value


def _attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(
        attempt_id=row["id"],
        target_id=row["target_id"],
        opened_at=_dt(row["opened_at"]),
        changes=_CHANGES.validate_json(row["changes_json"]),
        expires_at=_opt_dt(row["expires_at"]),
        closed_at=_opt_dt(row["closed_at"]),
        outcome=row["outcome"],
    )


def _audit(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        target_id=row["target_id"],
        attempt_id=row["attempt_id"],
        from_state=State(row["from_state"]),
        to_state=State(row["to_state"]),
        trigger=Trigger(row["event"]),
        reason=row["reason"],
        at=_dt(row["at"]),
    )


class Store:
    """Async access to the ShowWatch database. Create it with `await Store.open(...)`."""

    def __init__(
        self, conn: sqlite3.Connection, clock: Clock, new_attempt_id: Callable[[str], str]
    ) -> None:
        self._conn: sqlite3.Connection | None = conn
        self._clock = clock
        self._new_attempt_id = new_attempt_id
        self._lock = threading.Lock()

    @classmethod
    async def open(
        cls,
        path: Path,
        *,
        clock: Clock,
        new_attempt_id: Callable[[str], str] = _random_attempt_id,
    ) -> Self:
        try:
            conn = await asyncio.to_thread(_connect, path)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open the database {path.name}: {exc}") from exc
        return cls(conn, clock, new_attempt_id)

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _run[**P, R](
        self, fn: Callable[Concatenate[sqlite3.Connection, P], R], *args: P.args, **kwargs: P.kwargs
    ) -> R:
        def locked() -> R:
            with self._lock:
                if self._conn is None:
                    raise StoreError("the store is closed")
                try:
                    return fn(self._conn, *args, **kwargs)
                except sqlite3.Error as exc:  # callers only ever see typed errors (SPEC §5)
                    raise StoreError(f"database error: {exc}") from exc

        return await asyncio.to_thread(locked)

    # --- schema -------------------------------------------------------------------------------

    async def schema_version(self) -> int:
        return await self._run(lambda c: int(c.execute("PRAGMA user_version").fetchone()[0]))

    async def journal_mode(self) -> str:
        return await self._run(lambda c: str(c.execute("PRAGMA journal_mode").fetchone()[0]))

    # --- targets ------------------------------------------------------------------------------

    async def add_target(self, target: Target) -> TargetRecord:
        """Store a new target in IDLE. At most MAX_TARGETS unfinished targets may exist (C5)."""
        return await self._run(self._add_target, target)

    def _add_target(self, conn: sqlite3.Connection, target: Target) -> TargetRecord:
        now = self._clock.now()
        with _write(conn):
            if conn.execute("SELECT 1 FROM targets WHERE id = ?", (target.id,)).fetchone():
                raise DuplicateTarget(f"a target with id {target.id!r} already exists")
            unfinished = conn.execute(
                "SELECT COUNT(*) FROM targets WHERE state != ?", (State.BOOKED.value,)
            ).fetchone()[0]
            if unfinished >= limits.MAX_TARGETS:
                raise TargetLimitReached(
                    f"at most {limits.MAX_TARGETS} unfinished targets are allowed "
                    "(personal-scale limit, C5)"
                )
            conn.execute(
                "INSERT INTO targets (id, config_json, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (target.id, target.model_dump_json(), State.IDLE.value, _ts(now), _ts(now)),
            )
        return TargetRecord(target, State.IDLE, now, now)

    async def get_target(self, target_id: str) -> TargetRecord:
        return await self._run(self._get_target, target_id)

    def _get_target(self, conn: sqlite3.Connection, target_id: str) -> TargetRecord:
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise UnknownTarget(f"no target with id {target_id!r}")
        return self._target_record(row)

    async def list_targets(self) -> list[TargetRecord]:
        return await self._run(
            lambda c: [
                self._target_record(r) for r in c.execute("SELECT * FROM targets ORDER BY rowid")
            ]
        )

    @staticmethod
    def _target_record(row: sqlite3.Row) -> TargetRecord:
        return TargetRecord(
            target=Target.model_validate_json(row["config_json"]),
            state=State(row["state"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    # --- transitions --------------------------------------------------------------------------

    async def fire(
        self,
        target_id: str,
        trigger: Trigger,
        *,
        reason: str | None = None,
        changes: Sequence[Change] | None = None,
        expires_at: datetime | None = None,
    ) -> TransitionResult:
        """Apply `trigger` to the target atomically: state, attempt and audit row together.

        `changes` (at least one, all for this target) must accompany CHANGE_DETECTED, and only it.
        `expires_at`, the seat-hold expiry, may accompany SEATS_HELD or SUMMARY_REACHED.
        Raises IllegalTransition (and writes nothing) if the state machine forbids the trigger.
        """
        found = _check_changes(target_id, trigger, changes)
        if expires_at is not None:
            if trigger not in (Trigger.SEATS_HELD, Trigger.SUMMARY_REACHED):
                raise ValueError("expires_at may only accompany seats_held or summary_reached")
            _ts(expires_at)  # rejects naive datetimes
        return await self._run(self._fire, target_id, trigger, reason, found, expires_at)

    def _fire(
        self,
        conn: sqlite3.Connection,
        target_id: str,
        trigger: Trigger,
        reason: str | None,
        changes: tuple[Change, ...],
        expires_at: datetime | None,
    ) -> TransitionResult:
        with _write(conn):
            return self._apply(conn, target_id, trigger, reason, changes, expires_at)

    def _apply(
        self,
        conn: sqlite3.Connection,
        target_id: str,
        trigger: Trigger,
        reason: str | None,
        changes: tuple[Change, ...],
        expires_at: datetime | None,
    ) -> TransitionResult:
        """One transition. The caller holds the write transaction."""
        row = conn.execute(
            "SELECT config_json, state FROM targets WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise UnknownTarget(f"no target with id {target_id!r}")
        mode = Target.model_validate_json(row["config_json"]).mode
        previous = State(row["state"])
        new_state = StateMachine.next_state(previous, trigger, mode)
        now = self._clock.now()

        if StateMachine.opens_attempt(trigger):
            if not changes:  # fire() checks this; repeated for internal callers
                raise ValueError("change_detected needs at least one change")
            attempt_id: str | None = self._new_attempt_id(target_id)
            try:
                conn.execute(
                    "INSERT INTO attempts (id, target_id, opened_at, changes_json) "
                    "VALUES (?, ?, ?, ?)",
                    (attempt_id, target_id, _ts(now), _CHANGES.dump_json(changes).decode()),
                )
            except sqlite3.IntegrityError as exc:
                if "attempts.target_id" in str(exc):
                    raise AttemptConflict(
                        f"target {target_id!r} already has an open attempt"
                    ) from exc
                raise StoreError(f"could not record the attempt: {exc}") from exc
        else:
            open_row = conn.execute(
                "SELECT id FROM attempts WHERE target_id = ? AND closed_at IS NULL",
                (target_id,),
            ).fetchone()
            attempt_id = open_row["id"] if open_row else None

        if expires_at is not None:
            if attempt_id is None:
                raise StoreError(f"target {target_id!r} holds seats but has no open attempt")
            conn.execute(
                "UPDATE attempts SET expires_at = ? WHERE id = ?", (_ts(expires_at), attempt_id)
            )
        if attempt_id is not None and StateMachine.closes_attempt(new_state):
            conn.execute(
                "UPDATE attempts SET closed_at = ?, outcome = ? WHERE id = ?",
                (_ts(now), _outcome(previous, trigger), attempt_id),
            )
        conn.execute(
            "UPDATE targets SET state = ?, updated_at = ? WHERE id = ?",
            (new_state.value, _ts(now), target_id),
        )
        conn.execute(
            "INSERT INTO audit "
            "(target_id, attempt_id, from_state, to_state, event, reason, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                target_id,
                attempt_id,
                previous.value,
                new_state.value,
                trigger.value,
                reason,
                _ts(now),
            ),
        )
        return TransitionResult(target_id, attempt_id, previous, new_state, trigger, reason, now)

    # --- polling --------------------------------------------------------------------------------

    async def record_poll(
        self,
        target_id: str,
        *,
        snapshot: Snapshot,
        seen: Snapshot,
        changes: Sequence[Change],
    ) -> TransitionResult | None:
        """Save a successful poll; if it found changes, open the attempt in the same transaction.

        Only a WATCHING target detects changes and moves its baseline (`seen`) forward. If the
        target stopped watching during the poll (paused, say), its baseline is left alone, so the
        changes are found again once it watches again.
        Returns the CHANGE_DETECTED transition, or None.
        """
        for snap in (snapshot, seen):
            if snap.target_id != target_id:
                raise ValueError(f"snapshot is for {snap.target_id!r}, not {target_id!r}")
        found = _check_changes(target_id, Trigger.CHANGE_DETECTED, changes) if changes else ()
        return await self._run(self._record_poll, target_id, snapshot, seen, found)

    def _record_poll(
        self,
        conn: sqlite3.Connection,
        target_id: str,
        snapshot: Snapshot,
        seen: Snapshot,
        changes: tuple[Change, ...],
    ) -> TransitionResult | None:
        now = _ts(self._clock.now())
        with _write(conn):
            row = conn.execute("SELECT state FROM targets WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise UnknownTarget(f"no target with id {target_id!r}")
            watching = State(row["state"]) is State.WATCHING
            result = None
            if watching and changes:
                result = self._apply(conn, target_id, Trigger.CHANGE_DETECTED, None, changes, None)
            previous = conn.execute(
                "SELECT seen_json FROM watch WHERE target_id = ?", (target_id,)
            ).fetchone()
            seen_json = _snapshot_json(seen) if watching else (previous[0] if previous else None)
            conn.execute(
                "INSERT INTO watch"
                " (target_id, last_poll_at, last_success_at, snapshot_json, seen_json)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(target_id) DO UPDATE SET"
                " last_poll_at = excluded.last_poll_at,"
                " last_success_at = excluded.last_success_at,"
                " snapshot_json = excluded.snapshot_json,"
                " seen_json = excluded.seen_json",
                (target_id, now, now, _snapshot_json(snapshot), seen_json),
            )
        return result

    async def record_poll_failure(self, target_id: str) -> None:
        """Note that a poll was attempted and failed (for the heartbeat's "last poll")."""

        def write(conn: sqlite3.Connection) -> None:
            with _write(conn):
                conn.execute(
                    "INSERT INTO watch (target_id, last_poll_at) VALUES (?, ?) "
                    "ON CONFLICT(target_id) DO UPDATE SET last_poll_at = excluded.last_poll_at",
                    (target_id, _ts(self._clock.now())),
                )

        await self._run(write)

    async def watch_state(self, target_id: str) -> WatchState:
        def read(conn: sqlite3.Connection) -> WatchState:
            row = conn.execute("SELECT * FROM watch WHERE target_id = ?", (target_id,)).fetchone()
            if row is None:
                return WatchState()
            return WatchState(
                last_poll_at=_opt_dt(row["last_poll_at"]),
                last_success_at=_opt_dt(row["last_success_at"]),
                snapshot=_snapshot(row["snapshot_json"]),
                seen=_snapshot(row["seen_json"]),
            )

        return await self._run(read)

    # --- attempts and audit -------------------------------------------------------------------

    async def open_attempt(self, target_id: str) -> Attempt | None:
        return await self._run(self._open_attempt, target_id)

    @staticmethod
    def _open_attempt(conn: sqlite3.Connection, target_id: str) -> Attempt | None:
        row = conn.execute(
            "SELECT * FROM attempts WHERE target_id = ? AND closed_at IS NULL", (target_id,)
        ).fetchone()
        return None if row is None else _attempt(row)

    async def get_attempt(self, attempt_id: str) -> Attempt:
        return await self._run(self._get_attempt, attempt_id)

    @staticmethod
    def _get_attempt(conn: sqlite3.Connection, attempt_id: str) -> Attempt:
        row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise UnknownAttempt(f"no attempt with id {attempt_id!r}")
        return _attempt(row)

    async def attempts(self, target_id: str) -> list[Attempt]:
        return await self._run(
            lambda c: [
                _attempt(r)
                for r in c.execute(
                    "SELECT * FROM attempts WHERE target_id = ? ORDER BY rowid", (target_id,)
                )
            ]
        )

    async def audit(self, target_id: str | None = None) -> list[AuditEntry]:
        def query(conn: sqlite3.Connection) -> list[AuditEntry]:
            if target_id is None:
                rows = conn.execute("SELECT * FROM audit ORDER BY id")
            else:
                rows = conn.execute(
                    "SELECT * FROM audit WHERE target_id = ? ORDER BY id", (target_id,)
                )
            return [_audit(r) for r in rows]

        return await self._run(query)

    # --- notifications -------------------------------------------------------------------------

    async def notification_sent(self, event_id: str) -> bool:
        return await self._run(
            lambda c: (
                c.execute("SELECT 1 FROM notifications WHERE event_id = ?", (event_id,)).fetchone()
                is not None
            )
        )

    async def mark_notification_sent(self, event: Event) -> None:
        def write(conn: sqlite3.Connection) -> None:
            with _write(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO notifications (event_id, kind, target_id, sent_at) "
                    "VALUES (?, ?, ?, ?)",
                    (event.event_id, event.kind.value, event.target_id, _ts(self._clock.now())),
                )

        await self._run(write)

    # --- startup recovery ---------------------------------------------------------------------

    async def recover(self) -> Recovery:
        """Bring in-flight work to a safe state after a restart (SPEC §3). Idempotent.

        * DETECTED or SELECTING → WATCHING (`recover`); the attempts are reported as interrupted.
        * HELD or AWAITING_PAYMENT whose hold has expired, or whose expiry was never read
          → EXPIRED → WATCHING.
        * HELD or AWAITING_PAYMENT with time left stays as it is, and is reported so the
          countdown can be notified again.
        * EXPIRED or RELEASED (a crash before the automatic rewatch) → WATCHING.
        """
        return await self._run(self._recover)

    def _recover(self, conn: sqlite3.Connection) -> Recovery:
        now = self._clock.now()
        transitions: list[TransitionResult] = []
        interrupted: list[Attempt] = []
        running: list[Attempt] = []

        def fire(target_id: str, trigger: Trigger, reason: str) -> None:
            transitions.append(self._fire(conn, target_id, trigger, reason, (), None))

        rows = conn.execute("SELECT id, state FROM targets ORDER BY rowid").fetchall()
        for target_id, raw_state in rows:
            state = State(raw_state)
            attempt = self._open_attempt(conn, target_id)
            if state in (State.DETECTED, State.SELECTING):
                fire(target_id, Trigger.RECOVER, f"restarted while {state.value}")
                if attempt is not None:
                    interrupted.append(self._get_attempt(conn, attempt.attempt_id))
            elif state in (State.HELD, State.AWAITING_PAYMENT):
                expires = attempt.expires_at if attempt is not None else None
                if attempt is not None and expires is not None and expires > now:
                    running.append(attempt)
                    continue
                why = (
                    "hold expiry unknown after restart"
                    if expires is None
                    else "hold expired while ShowWatch was not running"
                )
                fire(target_id, Trigger.HOLD_EXPIRED, why)
                fire(target_id, Trigger.REWATCH, "restarted")
            elif state in (State.EXPIRED, State.RELEASED):
                fire(target_id, Trigger.REWATCH, "restarted")
        return Recovery(tuple(transitions), tuple(interrupted), tuple(running))
