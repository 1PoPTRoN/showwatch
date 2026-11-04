"""Message templates: Event → text. Pure (no I/O, no clock).

* Every time is shown as wall-clock time in `tz` (Asia/Kolkata for ShowWatch users), with day and
  month names from fixed tables, so the output never depends on the machine's locale.
* `html=True` produces Telegram HTML: everything taken from the config or the site is escaped,
  and only our own <b> and <code> tags remain. `html=False` produces plain text (console).
* The minutes left on a hold are rounded down, so the time is never overstated.
"""

import html as html_lib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from showwatch.models import Change, ChangeKind, Event, EventKind, Show

MAX_LENGTH = 4096  # Telegram's limit for one message
MAX_SHOWS_PER_DATE = 6
MAX_REASON = 300  # free-text reasons are clipped so one line can never swamp a message

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_BLOCK_REASONS = {
    "captcha": "a CAPTCHA",
    "queue": "a waiting queue",
    "forbidden": "a block page (403)",
}
_FAILURE_REASONS = {
    "seat_race_exhausted": "the chosen seats kept being taken by others before they could be held",
    "needs_login": "you are logged out of the site",
    "locator_not_found": "the site's pages have changed (a button ShowWatch needs was not found)",
}


def _clip(text: str | None, limit: int = MAX_REASON) -> str:
    value = text or ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass(frozen=True)
class _Fmt:
    tz: ZoneInfo
    html: bool

    def esc(self, text: object) -> str:
        value = str(text)
        return html_lib.escape(value, quote=False) if self.html else value

    def bold(self, text: object) -> str:
        return f"<b>{self.esc(text)}</b>" if self.html else str(text)

    def code(self, text: object) -> str:
        return f"<code>{self.esc(text)}</code>" if self.html else str(text)

    def day(self, value: date) -> str:
        return f"{_DAYS[value.weekday()]} {value.day} {_MONTHS[value.month - 1]}"

    def clock(self, moment: datetime) -> str:
        local = moment.astimezone(self.tz)
        hour = local.hour % 12 or 12
        return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"

    def when(self, moment: datetime) -> str:
        return f"{self.day(moment.astimezone(self.tz).date())}, {self.clock(moment)}"

    def show_line(self, show: Show) -> str:
        when = self.clock(show.starts_at)
        return self.esc(f"{show.venue}, {when}, {show.screen_format}, from ₹{show.min_price}")

    def movie(self, event: Event) -> str:
        return self.bold(event.movie or event.target_id or "ShowWatch")


def local_clock(moment: datetime, tz: ZoneInfo) -> str:
    """Wall-clock time in `tz`, like "7:05 PM"."""
    return _Fmt(tz, html=False).clock(moment)


def _changes(f: _Fmt, changes: tuple[Change, ...]) -> list[str]:
    lines: list[str] = []
    for change in changes:
        label = "opened" if change.kind is ChangeKind.DATE_OPENED else "new shows"
        lines.append(f"{f.bold(f.day(change.on_date))} ({label}):")
        shown = change.shows[:MAX_SHOWS_PER_DATE]
        lines += [f"• {f.show_line(s)}" for s in shown]
        if len(change.shows) > len(shown):
            lines.append(f"• and {len(change.shows) - len(shown)} more")
    return lines


def _hold(f: _Fmt, event: Event) -> str:
    if event.expires_at is None:
        return "The hold time is unknown, so pay as soon as you can."
    left = (event.expires_at - event.at).total_seconds()
    if left <= 0:
        return f"Pay by {f.clock(event.expires_at)}: the hold has run out."
    minutes = int(left // 60)
    remaining = "less than a minute left" if minutes < 1 else f"{minutes} min left"
    return f"Pay by {f.bold(f.clock(event.expires_at))} ({remaining})."


def _seats(f: _Fmt, event: Event) -> str:
    return f.bold(", ".join(event.seats)) if event.seats else "your seats"


def _show(f: _Fmt, event: Event) -> str:
    if event.show is None:
        return ""
    s = event.show
    return f" at {f.esc(s.venue)}, {f.esc(f.when(s.starts_at))} ({f.esc(s.screen_format)})"


def _detected(f: _Fmt, e: Event) -> list[str]:
    return [f"Booking open for {f.movie(e)}!", *_changes(f, e.changes)]


def _already_open(f: _Fmt, e: Event) -> list[str]:
    return [
        f"{f.movie(e)}: booking is already open for shows that match your rules.",
        *_changes(f, e.changes),
        "ShowWatch will alert you when new ones appear.",
    ]


def _blocked(f: _Fmt, e: Event) -> list[str]:
    what = _BLOCK_REASONS.get(e.reason or "", "a block page")
    return [
        f"{f.movie(e)}: paused. The site showed {f.esc(what)}, which was not bypassed.",
        "Deal with it in the browser if you like, then run "
        f"{f.code(f'showwatch resume {e.target_id}')}.",
    ]


def _poll_failing(f: _Fmt, e: Event) -> list[str]:
    return [
        f"{f.movie(e)}: availability checks have failed 5 times in a row "
        f"({f.esc(_clip(e.reason))}).",
        "ShowWatch keeps trying, with longer gaps.",
    ]


def _no_match(f: _Fmt, e: Event) -> list[str]:
    return [
        f"{f.movie(e)}: booking opened, but no seats match your rules"
        + (f" ({f.esc(_clip(e.reason))})." if e.reason else "."),
        *_changes(f, e.changes),
    ]


def _dry_run(f: _Fmt, e: Event) -> list[str]:
    return [
        f"{f.movie(e)}: dry run done. ShowWatch would pick {_seats(f, e)}{_show(f, e)}.",
        "Nothing was clicked. A screenshot with the seats highlighted was saved.",
    ]


def _held(f: _Fmt, e: Event) -> list[str]:
    return [
        f"Seats held for {f.movie(e)}: {_seats(f, e)}{_show(f, e)}.",
        _hold(f, e),
        "Finish the payment yourself in the open browser window. ShowWatch will not pay for you.",
        f"When you are done: {f.code(f'showwatch done {e.attempt_id}')}",
    ]


def _expired(f: _Fmt, e: Event) -> list[str]:
    return [f"{f.movie(e)}: the hold on {_seats(f, e)} ran out. Watching again."]


def _failed(f: _Fmt, e: Event) -> list[str]:
    why = _FAILURE_REASONS.get(e.reason or "") or _clip(e.reason) or "an unknown error"
    lines = [f"{f.movie(e)}: could not finish: {f.esc(why)}."]
    if e.reason == "needs_login":
        lines.append(
            f"Run {f.code('showwatch login')}, then {f.code(f'showwatch resume {e.target_id}')}."
        )
    else:
        lines.append(f"Run {f.code(f'showwatch resume {e.target_id}')} to watch again.")
    return lines


def _interrupted(f: _Fmt, e: Event) -> list[str]:
    return [
        f"{f.movie(e)}: ShowWatch was stopped while choosing seats, so nothing is held. "
        "It is watching again.",
        *_changes(f, e.changes),
    ]


def _dates_passed(f: _Fmt, e: Event) -> list[str]:
    return [
        f"{f.movie(e)}: all of its dates have passed, so it is paused.",
        f"Remove it with {f.code(f'showwatch remove {e.target_id}')}, "
        "or add it again with new dates.",
    ]


def _canary_failed(f: _Fmt, e: Event) -> list[str]:
    return [
        f"{f.movie(e)}: the canary check failed at step {f.code(_clip(e.reason))}: "
        "the site's pages may have changed.",
        "Seat holding would fail too. Check the site, then run "
        f"{f.code('showwatch canary')} again.",
    ]


def _heartbeat(f: _Fmt, e: Event) -> list[str]:
    lines = [f"ShowWatch is running ({f.esc(f.when(e.at))})."]
    if not e.statuses:
        lines.append("No targets yet.")
    for status in e.statuses:
        last = (
            f"last checked {f.clock(status.last_poll_at)}"
            if status.last_poll_at is not None
            else "not checked yet"
        )
        lines.append(
            f"• {f.esc(status.target_id)} ({f.esc(status.movie)}): {f.esc(status.state)}, {last}"
        )
    return lines


_TEMPLATES: dict[EventKind, Callable[[_Fmt, Event], list[str]]] = {
    EventKind.DETECTED: _detected,
    EventKind.ALREADY_OPEN: _already_open,
    EventKind.BLOCKED: _blocked,
    EventKind.POLL_FAILING: _poll_failing,
    EventKind.NO_MATCH: _no_match,
    EventKind.DRY_RUN: _dry_run,
    EventKind.HELD: _held,
    EventKind.EXPIRED: _expired,
    EventKind.FAILED: _failed,
    EventKind.HEARTBEAT: _heartbeat,
    EventKind.INTERRUPTED: _interrupted,
    EventKind.DATES_PASSED: _dates_passed,
    EventKind.CANARY_FAILED: _canary_failed,
}


def render(event: Event, *, tz: ZoneInfo, html: bool) -> str:
    """The message for `event`, at most MAX_LENGTH characters."""
    text = "\n".join(_TEMPLATES[event.kind](_Fmt(tz, html), event))
    if len(text) <= MAX_LENGTH:
        return text
    # Cut on a line boundary, so no HTML tag is left half-open.
    kept: list[str] = []
    budget = MAX_LENGTH - len("\n…")
    for line in text.split("\n"):
        if sum(len(k) + 1 for k in kept) + len(line) > budget:
            break
        kept.append(line)
    return "\n".join(kept) + "\n…" if kept else "…"
