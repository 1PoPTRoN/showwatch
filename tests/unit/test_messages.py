"""Phase 5: message templates (SPEC §3 Notifier; test T5.2).

Every user-facing time is shown in Asia/Kolkata. The HTML form (Telegram) escapes everything
that came from the config or the site.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from showwatch.models import Change, ChangeKind, Event, EventKind, TargetStatus
from showwatch.notify.messages import MAX_LENGTH, render
from tests.support.shows import FRI, SAT, show

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 3, 3, 30, tzinfo=UTC)  # 09:00 IST
SHOW = show("s1", venue="Cinema A", fmt="IMAX 2D", at="19:00", price=450)  # Fri 18 Dec, 7 PM IST


def event(kind: EventKind, **fields: object) -> Event:
    base: dict[str, object] = {
        "event_id": f"x:{kind}",
        "kind": kind,
        "at": NOW,
        "target_id": "movie-fri",
        "movie": "Test Movie",
        "attempt_id": "movie-fri-abc123",
    }
    base.update(fields)
    return Event(**base)  # type: ignore[arg-type]


HELD = event(
    EventKind.HELD,
    show=SHOW,
    seats=("H9", "H10"),
    expires_at=NOW + timedelta(minutes=8, seconds=30),
)


def test_t5_2_held_message_has_everything_needed_to_pay_in_time() -> None:
    text = render(HELD, tz=IST, html=False)
    assert "Test Movie" in text
    assert "H9, H10" in text  # seats
    assert "Cinema A" in text  # venue
    assert "Fri 18 Dec, 7:00 PM" in text  # show time in IST (13:30 UTC)
    assert "IMAX 2D" in text
    assert "Pay by 9:08 AM" in text  # hold expiry in IST
    assert "8 min left" in text  # minutes left, rounded down so it is never overstated
    assert "showwatch done movie-fri-abc123" in text
    assert "will not pay" in text  # C1: payment is always the user's


@pytest.mark.parametrize(
    ("left", "expected"),
    [
        (timedelta(seconds=40), "less than a minute left"),
        (timedelta(seconds=0), "the hold has run out"),
        (timedelta(minutes=-2), "the hold has run out"),
    ],
)
def test_held_message_near_and_after_expiry(left: timedelta, expected: str) -> None:
    text = render(
        event(EventKind.HELD, show=SHOW, seats=("H9",), expires_at=NOW + left), tz=IST, html=False
    )
    assert expected in text


def test_detected_lists_dates_and_shows_in_local_time() -> None:
    changes = (
        Change(ChangeKind.DATE_OPENED, "movie-fri", FRI, (SHOW,)),
        Change(
            ChangeKind.SHOW_ADDED,
            "movie-fri",
            SAT,
            (show("s2", on=SAT, venue="Cinema B", at="21:15"),),
        ),
    )
    text = render(event(EventKind.DETECTED, changes=changes), tz=IST, html=False)
    assert "Booking open" in text
    assert "Fri 18 Dec" in text
    assert "Cinema A, 7:00 PM, IMAX 2D, from ₹450" in text
    assert "Sat 19 Dec" in text
    assert "Cinema B, 9:15 PM" in text


def test_long_show_lists_are_cut_short() -> None:
    many = tuple(show(f"s{i}", at=f"{18 + i // 6}:{(i % 6) * 10:02d}") for i in range(20))
    text = render(
        event(
            EventKind.DETECTED, changes=(Change(ChangeKind.DATE_OPENED, "movie-fri", FRI, many),)
        ),
        tz=IST,
        html=False,
    )
    assert "and 14 more" in text


def test_blocked_message_says_nothing_was_bypassed_and_how_to_resume() -> None:
    text = render(event(EventKind.BLOCKED, reason="captcha"), tz=IST, html=False)
    assert "CAPTCHA" in text
    assert "not bypassed" in text  # C3
    assert "showwatch resume movie-fri" in text


def test_failed_needs_login_tells_the_user_to_log_in() -> None:
    text = render(event(EventKind.FAILED, reason="needs_login"), tz=IST, html=False)
    assert "showwatch login" in text


def test_t5_7_heartbeat_lists_every_target() -> None:
    statuses = (
        TargetStatus("movie-fri", "Test Movie", "WATCHING", NOW - timedelta(minutes=2)),
        TargetStatus("movie-sat", "Other Movie", "PAUSED", None),
    )
    text = render(
        Event(event_id="hb", kind=EventKind.HEARTBEAT, at=NOW, statuses=statuses),
        tz=IST,
        html=False,
    )
    assert "movie-fri" in text
    assert "WATCHING" in text
    assert "last checked 8:58 AM" in text
    assert "movie-sat" in text
    assert "PAUSED" in text
    assert "not checked yet" in text


@pytest.mark.parametrize("kind", list(EventKind))
@pytest.mark.parametrize("html", [False, True])
def test_every_kind_renders(kind: EventKind, html: bool) -> None:
    text = render(
        event(
            kind,
            show=SHOW,
            seats=("H9",),
            expires_at=NOW + timedelta(minutes=5),
            reason="captcha" if kind is EventKind.BLOCKED else "seat_race_exhausted",
            changes=(Change(ChangeKind.DATE_OPENED, "movie-fri", FRI, (SHOW,)),),
            statuses=(TargetStatus("movie-fri", "Test Movie", "WATCHING", NOW),),
        ),
        tz=IST,
        html=html,
    )
    assert text.strip()
    assert len(text) <= MAX_LENGTH
    if kind is not EventKind.HEARTBEAT:
        assert "Test Movie" in text


def test_html_escapes_everything_from_config_and_site() -> None:
    nasty = show("s<1>", venue="Fun & <Games>", at="19:00")
    e = event(
        EventKind.HELD,
        movie="Tom & Jerry <3",
        show=nasty,
        seats=("<H9>",),
        expires_at=NOW + timedelta(minutes=5),
    )
    html_text = render(e, tz=IST, html=True)
    assert "Tom &amp; Jerry &lt;3" in html_text
    assert "Fun &amp; &lt;Games&gt;" in html_text
    assert "&lt;H9&gt;" in html_text
    stripped = (
        html_text.replace("<b>", "")
        .replace("</b>", "")
        .replace("<code>", "")
        .replace("</code>", "")
    )
    assert "<" not in stripped  # only our own tags survive
    assert "Tom & Jerry <3" in render(e, tz=IST, html=False)


def test_overlong_messages_are_truncated() -> None:
    e = event(EventKind.FAILED, reason="x" * 10_000)
    assert len(render(e, tz=IST, html=True)) <= MAX_LENGTH


def test_dates_render_without_depending_on_the_locale() -> None:
    sunday = date(2026, 12, 20)
    opened = Change(ChangeKind.DATE_OPENED, "movie-fri", sunday, (show("sun", on=sunday),))
    text = render(event(EventKind.DETECTED, changes=(opened,)), tz=IST, html=False)
    assert "Sun 20 Dec" in text
