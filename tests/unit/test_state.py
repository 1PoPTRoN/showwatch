"""Phase 3: the pure state machine (SPEC §3; tests T3.2 and T3.6 at the pure level).

The store tests (tests/integration/test_store.py) cover the same rules end to end: audit rows,
attempts, concurrency and recovery.
"""

import itertools

import pytest

from showwatch.config import Mode
from showwatch.errors import IllegalTransition
from showwatch.state import GUARDS, TERMINAL, TRANSITIONS, State, StateMachine, Trigger

ALL_MODES = tuple(Mode)

# SPEC §3, written out independently of state.py so the two are compared rather than shared.
SPEC_TABLE = {
    (State.IDLE, Trigger.START): State.WATCHING,
    (State.WATCHING, Trigger.CHANGE_DETECTED): State.DETECTED,
    (State.WATCHING, Trigger.BLOCKED): State.PAUSED,
    (State.DETECTED, Trigger.NOTIFIED): State.WATCHING,
    (State.DETECTED, Trigger.BEGIN_SELECT): State.SELECTING,
    (State.SELECTING, Trigger.DRY_RUN_DONE): State.WATCHING,
    (State.SELECTING, Trigger.NO_MATCHING_SEATS): State.WATCHING,
    (State.SELECTING, Trigger.SEATS_HELD): State.HELD,
    (State.SELECTING, Trigger.BLOCKED): State.PAUSED,
    (State.SELECTING, Trigger.ERROR): State.FAILED,
    (State.HELD, Trigger.SUMMARY_REACHED): State.AWAITING_PAYMENT,
    (State.AWAITING_PAYMENT, Trigger.USER_DONE): State.BOOKED,
    (State.AWAITING_PAYMENT, Trigger.HOLD_EXPIRED): State.EXPIRED,
    (State.HELD, Trigger.USER_CANCEL): State.RELEASED,
    (State.AWAITING_PAYMENT, Trigger.USER_CANCEL): State.RELEASED,
    (State.EXPIRED, Trigger.REWATCH): State.WATCHING,
    (State.RELEASED, Trigger.REWATCH): State.WATCHING,
    (State.PAUSED, Trigger.USER_RESUME): State.WATCHING,
    (State.FAILED, Trigger.USER_RESUME): State.WATCHING,
}
# Approved additions (docs/DESIGN-NOTES.md P3.2, P3.5, P8.2).
ADDED = {
    (State.DETECTED, Trigger.RECOVER): State.WATCHING,
    (State.SELECTING, Trigger.RECOVER): State.WATCHING,
    (State.HELD, Trigger.HOLD_EXPIRED): State.EXPIRED,
    (State.IDLE, Trigger.USER_PAUSE): State.PAUSED,
    (State.WATCHING, Trigger.USER_PAUSE): State.PAUSED,
    (State.IDLE, Trigger.DATES_PASSED): State.PAUSED,
    (State.WATCHING, Trigger.DATES_PASSED): State.PAUSED,
}


def test_table_is_exactly_the_spec_plus_the_approved_additions() -> None:
    assert dict(TRANSITIONS) == SPEC_TABLE | ADDED


def test_states_and_triggers_match_the_spec_names() -> None:
    assert [s.value for s in State] == [
        "IDLE",
        "WATCHING",
        "DETECTED",
        "SELECTING",
        "HELD",
        "AWAITING_PAYMENT",
        "BOOKED",
        "RELEASED",
        "EXPIRED",
        "FAILED",
        "PAUSED",
    ]
    assert Trigger.CHANGE_DETECTED.value == "change_detected"
    assert Trigger.USER_DONE.value == "user_done"


@pytest.mark.parametrize(("key", "expected"), sorted(TRANSITIONS.items()))
def test_every_legal_transition(key: tuple[State, Trigger], expected: State) -> None:
    state, trigger = key
    mode = sorted(GUARDS.get(key, ALL_MODES))[0]
    assert StateMachine.next_state(state, trigger, mode) is expected


ILLEGAL = sorted(
    (state, trigger)
    for state, trigger in itertools.product(State, Trigger)
    if (state, trigger) not in TRANSITIONS
)


@pytest.mark.parametrize(("state", "trigger"), ILLEGAL)
def test_t3_2_every_other_combination_is_illegal(state: State, trigger: Trigger) -> None:
    for mode in ALL_MODES:
        with pytest.raises(IllegalTransition, match=trigger.value):
            StateMachine.next_state(state, trigger, mode)


@pytest.mark.parametrize("trigger", list(Trigger))
def test_t3_6_booked_is_terminal(trigger: Trigger) -> None:
    assert State.BOOKED in TERMINAL
    for mode in ALL_MODES:
        with pytest.raises(IllegalTransition):
            StateMachine.next_state(State.BOOKED, trigger, mode)


# --- mode guards: what each mode may never do ------------------------------------------------


@pytest.mark.parametrize(
    ("state", "trigger", "allowed"),
    [
        (State.DETECTED, Trigger.NOTIFIED, {Mode.NOTIFY_ONLY}),
        (State.DETECTED, Trigger.BEGIN_SELECT, {Mode.DRY_RUN, Mode.ASSIST}),
        (State.SELECTING, Trigger.DRY_RUN_DONE, {Mode.DRY_RUN}),
        (State.SELECTING, Trigger.SEATS_HELD, {Mode.ASSIST}),
    ],
)
def test_mode_guards(state: State, trigger: Trigger, allowed: set[Mode]) -> None:
    for mode in ALL_MODES:
        if mode in allowed:
            StateMachine.next_state(state, trigger, mode)
        else:
            with pytest.raises(IllegalTransition, match=mode.value):
                StateMachine.next_state(state, trigger, mode)


def test_notify_only_can_never_reach_seat_selection() -> None:
    # Breadth-first search of everything a notify-only target can reach (T8.1: no browser).
    reachable, frontier = {State.IDLE}, [State.IDLE]
    while frontier:
        state = frontier.pop()
        for trigger in Trigger:
            try:
                nxt = StateMachine.next_state(state, trigger, Mode.NOTIFY_ONLY)
            except IllegalTransition:
                continue
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == {State.IDLE, State.WATCHING, State.DETECTED, State.PAUSED}


def test_dry_run_can_never_hold_seats() -> None:
    reachable, frontier = {State.IDLE}, [State.IDLE]
    while frontier:
        state = frontier.pop()
        for trigger in Trigger:
            try:
                nxt = StateMachine.next_state(state, trigger, Mode.DRY_RUN)
            except IllegalTransition:
                continue
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert not reachable & {State.HELD, State.AWAITING_PAYMENT, State.BOOKED}
