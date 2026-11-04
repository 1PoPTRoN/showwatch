"""HTML for the fake site: `string.Template` files plus escaped fragments. No scripts, no assets.

Every dynamic value is escaped here before it reaches a template.
"""

from datetime import date, datetime
from html import escape
from pathlib import Path
from string import Template

from tests.fakesite.state import IST, FakeShow, SiteState

_TEMPLATES = Path(__file__).parent / "templates"
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _template(name: str) -> Template:
    return Template((_TEMPLATES / f"{name}.html").read_text(encoding="utf-8"))


def page(name: str, title: str, attrs: dict[str, str], **values: str) -> str:
    content = _template(name).substitute(values)
    attr_text = " ".join(f'{k}="{escape(v)}"' for k, v in attrs.items())
    return _template("base").substitute(title=escape(title), attrs=attr_text, content=content)


def day_label(value: date) -> str:
    return f"{_DAYS[value.weekday()]} {value.day} {_MONTHS[value.month - 1]}"


def clock_label(moment: datetime) -> str:
    local = moment.astimezone(IST)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def show_line(show: FakeShow) -> str:
    return escape(
        f"{show.venue} · {day_label(show.on)}, {clock_label(show.starts_at)} · {show.screen_format}"
    )


def movie_page(state: SiteState) -> str:
    booking = (
        f'<p><a href="/movie/shows">{escape(state.name("book"))}</a></p>'
        if state.booking_open
        else "<p>Coming soon</p>"
    )
    return page(
        "movie",
        state.movie,
        {"data-page": "movie", "data-booking": "open" if state.booking_open else "soon"},
        movie=escape(state.movie),
        booking=booking,
    )


def listing_page(state: SiteState) -> str:
    sections = []
    for day in state.open_dates():
        venues: dict[str, list[FakeShow]] = {}
        for show in sorted(
            (s for s in state.shows if s.on == day), key=lambda s: (s.at, s.show_id)
        ):
            venues.setdefault(show.venue, []).append(show)
        blocks = []
        for venue, shows in venues.items():
            items = "\n".join(
                f'      <li><a href="/show/{escape(s.show_id)}" data-show-id="{escape(s.show_id)}" '
                f'data-venue="{escape(s.venue)}" data-format="{escape(s.screen_format)}" '
                f'data-language="{escape(s.language)}" data-starts-at="{s.starts_at.isoformat()}" '
                f'data-min-price="{s.min_price}">{escape(clock_label(s.starts_at))} · '
                f"{escape(s.screen_format)} · from ₹{s.min_price}</a></li>"
                for s in shows
            )
            blocks.append(
                f'    <section aria-label="{escape(venue)}" data-venue="{escape(venue)}">\n'
                f"      <h4>{escape(venue)}</h4>\n      <ul>\n{items}\n      </ul>\n    </section>"
            )
        sections.append(
            f'<section aria-label="{day_label(day)}" data-date="{day.isoformat()}">\n'
            f"  <h3>{day_label(day)}</h3>\n" + "\n".join(blocks) + "\n</section>"
        )
    dates = "\n".join(sections) if sections else "<p>Coming soon</p>"
    return page(
        "listing",
        state.movie,
        {"data-page": "listing", "data-booking": "open" if state.booking_open else "soon"},
        movie=escape(state.movie),
        dates=dates,
    )


def quantity_page(state: SiteState, show: FakeShow) -> str:
    options = "".join(
        f'<option value="{n}"{" selected" if n == 2 else ""}>{n}</option>' for n in range(1, 11)
    )
    return page(
        "quantity",
        f"{state.movie} · seats",
        {"data-page": "quantity", "data-show-id": show.show_id},
        movie=escape(state.movie),
        show_line=show_line(show),
        show_id=escape(show.show_id),
        options=options,
        select_label=escape(state.name("select_seats")),
    )


def seats_page(
    state: SiteState,
    show: FakeShow,
    *,
    qty: int,
    selected: list[str],
    unavailable: set[str],
    notice: str,
) -> str:
    rows = []
    for row in state.layout:
        if not row:
            continue
        cells = []
        for cell in row:
            if cell.label is None:
                cells.append(
                    f'  <span data-row="{escape(cell.row)}" data-col="{cell.col}" data-gap="true" '
                    'aria-hidden="true"></span>'
                )
                continue
            taken = cell.label in unavailable
            pressed = "true" if cell.label in selected else "false"
            cells.append(
                f'  <button type="submit" form="select" name="seat" value="{escape(cell.label)}" '
                f'data-row="{escape(cell.row)}" data-col="{cell.col}" '
                f'data-testid="seat-{escape(cell.row)}-{cell.col}" '
                f'aria-label="{escape(cell.label)}" '
                f'aria-pressed="{pressed}"'
                + (' data-taken="true" disabled' if taken else "")
                + f">{escape(cell.label)}</button>"
            )
        label = escape(row[0].row)
        rows.append(
            f'<div role="row" aria-label="Row {label}" data-row="{label}">\n'
            + "\n".join(cells)
            + "\n</div>"
        )
    total = qty * show.min_price
    return page(
        "seats",
        f"{state.movie} · seat map",
        {"data-page": "seats", "data-show-id": show.show_id, "data-qty": str(qty)},
        qty=str(qty),
        show_line=show_line(show),
        notice=f'<p role="alert">{escape(notice)}</p>' if notice else "",
        show_id=escape(show.show_id),
        selected=escape(",".join(selected)),
        rows="\n".join(rows),
        disabled="" if len(selected) == qty else " disabled",
        proceed_label=escape(state.name("proceed_to_summary", total=total)),
    )


def summary_page(
    state: SiteState, show: FakeShow, hold_id: str, seats: tuple[str, ...], left_s: int
) -> str:
    return page(
        "summary",
        "Order summary",
        {"data-page": "summary", "data-hold-id": hold_id},
        movie=escape(state.movie),
        venue=escape(show.venue),
        when=escape(f"{day_label(show.on)}, {clock_label(show.starts_at)} · {show.screen_format}"),
        seats=escape(", ".join(seats)),
        total=str(len(seats) * show.min_price),
        seconds=str(left_s),
        countdown=f"{left_s // 60:02d}:{left_s % 60:02d}",
        hold_id=escape(hold_id),
        continue_label=escape(state.name("continue_to_payment")),
    )


def payment_page(state: SiteState, show: FakeShow, hold_id: str, seats: tuple[str, ...]) -> str:
    return page(
        "payment",
        "Payment",
        {"data-page": "payment", "data-hold-id": hold_id},
        total=str(len(seats) * show.min_price),
        seats=escape(", ".join(seats)),
        hold_id=escape(hold_id),
        pay_label=escape(state.name("pay_now")),
    )


def confirmation_page(seats: tuple[str, ...]) -> str:
    return page(
        "confirmation",
        "Booking confirmed",
        {"data-page": "confirmation"},
        seats=escape(", ".join(seats)),
    )


INTERSTITIAL_TEXT = {
    "captcha": ("Verify you are human", "Complete the check to continue."),
    "queue": ("You are in the queue", "Please wait. You will be let in when it is your turn."),
    "login": ("Sign in", "Sign in to continue booking."),
}


def interstitial_page(kind: str) -> str:
    heading, text = INTERSTITIAL_TEXT[kind]
    return page(
        "message",
        heading,
        {"data-page": "interstitial", "data-interstitial": kind},
        heading=escape(heading),
        text=escape(text),
    )


def message_page(kind: str, heading: str, text: str) -> str:
    return page("message", heading, {"data-page": kind}, heading=escape(heading), text=escape(text))
