"""The booking state machine (SPEC §3). Pure: no I/O and no clock.

`store.Store.fire()` is the only caller that changes a target's state. It applies this machine
inside a database transaction.

Beyond the §3 table, the machine has:
  * approved additions: `recover` (DETECTED or SELECTING → WATCHING, used on startup),
    `hold_expired` from HELD, `user_pause` for the `showwatch pause` command, and `dates_passed`
    (a target whose dates have all gone by stops itself; docs/DESIGN-NOTES.md P3.2, P3.5, P8.2);
  * mode guards: a notify-only target can never start seat selection, and a dry-run target can
    never hold seats. These are enforced here, not left to the caller's discipline.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from showwatch.config import Mode
from showwatch.errors import IllegalTransition


class State(StrEnum):
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    DETECTED = "DETECTED"
    SELECTING = "SELECTING"
    HELD = "HELD"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    BOOKED = "BOOKED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class Trigger(StrEnum):
    """What happened. This is the "Event" column of the SPEC §3 table.

    It is named Trigger so it is not confused with notification events.
    """

    START = "start"
    CHANGE_DETECTED = "change_detected"
    BLOCKED = "blocked"
    NOTIFIED = "notified"
    BEGIN_SELECT = "begin_select"
    DRY_RUN_DONE = "dry_run_done"
    NO_MATCHING_SEATS = "no_matching_seats"
    SEATS_HELD = "seats_held"
    ERROR = "error"
    SUMMARY_REACHED = "summary_reached"
    USER_DONE = "user_done"
    HOLD_EXPIRED = "hold_expired"
    USER_CANCEL = "user_cancel"
    REWATCH = "rewatch"
    USER_RESUME = "user_resume"
    RECOVER = "recover"
    USER_PAUSE = "user_pause"
    DATES_PASSED = "dates_passed"


_S, _T = State, Trigger

TRANSITIONS: Mapping[tuple[State, Trigger], State] = MappingProxyType(
    {
        # SPEC §3
        (_S.IDLE, _T.START): _S.WATCHING,
        (_S.WATCHING, _T.CHANGE_DETECTED): _S.DETECTED,
        (_S.WATCHING, _T.BLOCKED): _S.PAUSED,
        (_S.DETECTED, _T.NOTIFIED): _S.WATCHING,
        (_S.DETECTED, _T.BEGIN_SELECT): _S.SELECTING,
        (_S.SELECTING, _T.DRY_RUN_DONE): _S.WATCHING,
        (_S.SELECTING, _T.NO_MATCHING_SEATS): _S.WATCHING,
        (_S.SELECTING, _T.SEATS_HELD): _S.HELD,
        (_S.SELECTING, _T.BLOCKED): _S.PAUSED,
        (_S.SELECTING, _T.ERROR): _S.FAILED,
        (_S.HELD, _T.SUMMARY_REACHED): _S.AWAITING_PAYMENT,
        (_S.AWAITING_PAYMENT, _T.USER_DONE): _S.BOOKED,
        (_S.AWAITING_PAYMENT, _T.HOLD_EXPIRED): _S.EXPIRED,
        (_S.HELD, _T.USER_CANCEL): _S.RELEASED,
        (_S.AWAITING_PAYMENT, _T.USER_CANCEL): _S.RELEASED,
        (_S.EXPIRED, _T.REWATCH): _S.WATCHING,
        (_S.RELEASED, _T.REWATCH): _S.WATCHING,
        (_S.PAUSED, _T.USER_RESUME): _S.WATCHING,
        (_S.FAILED, _T.USER_RESUME): _S.WATCHING,
        # Approved additions
        (_S.DETECTED, _T.RECOVER): _S.WATCHING,
        (_S.SELECTING, _T.RECOVER): _S.WATCHING,
        (_S.HELD, _T.HOLD_EXPIRED): _S.EXPIRED,
        (_S.IDLE, _T.USER_PAUSE): _S.PAUSED,
        (_S.WATCHING, _T.USER_PAUSE): _S.PAUSED,
        (_S.IDLE, _T.DATES_PASSED): _S.PAUSED,
        (_S.WATCHING, _T.DATES_PASSED): _S.PAUSED,
    }
)

GUARDS: Mapping[tuple[State, Trigger], frozenset[Mode]] = MappingProxyType(
    {
        (_S.DETECTED, _T.NOTIFIED): frozenset({Mode.NOTIFY_ONLY}),
        (_S.DETECTED, _T.BEGIN_SELECT): frozenset({Mode.DRY_RUN, Mode.ASSIST}),
        (_S.SELECTING, _T.DRY_RUN_DONE): frozenset({Mode.DRY_RUN}),
        (_S.SELECTING, _T.SEATS_HELD): frozenset({Mode.ASSIST}),
    }
)

TERMINAL = frozenset({State.BOOKED})


class StateMachine:
    @staticmethod
    def next_state(state: State, trigger: Trigger, mode: Mode) -> State:
        """The state after `trigger`; IllegalTransition if the table or a guard forbids it."""
        key = (state, trigger)
        if key not in TRANSITIONS:
            terminal = " (a terminal state)" if state in TERMINAL else ""
            raise IllegalTransition(
                f"{trigger.value} is not allowed in state {state.value}{terminal}"
            )
        allowed = GUARDS.get(key)
        if allowed is not None and mode not in allowed:
            raise IllegalTransition(
                f"{trigger.value} is not allowed for a {mode.value} target (state {state.value})"
            )
        return TRANSITIONS[key]

    @staticmethod
    def opens_attempt(trigger: Trigger) -> bool:
        return trigger is Trigger.CHANGE_DETECTED

    @staticmethod
    def closes_attempt(new_state: State) -> bool:
        """An attempt ends when its target goes back to watching, or is booked."""
        return new_state in (State.WATCHING, State.BOOKED)
