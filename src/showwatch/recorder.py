"""The calibration recorder (Phase 10): turns the user's own clicks into a draft calibration file.

The user drives a headed browser through one booking, as far as the payment page, and pays
nothing. The recorder only listens. It never clicks, types or navigates, apart from opening the
start page, so it sends nothing the user did not cause. For each step it suggests a locator the
way SPEC §5 wants (role and accessible name first, then test id, then exact text, T10.3) and
`draft_yaml` writes it all out for the user to check. `scripts/calibrate.py` is the command.

Never recorded (C2, C7): text fields or anything typed into them, `value` attributes (except a
button's label), token-like attributes, and query strings (except a date) or fragments.
"""

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlsplit

import yaml
from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from showwatch.adapters.aria import name_from, normalise, role_of
from showwatch.adapters.base import LocatorSpec
from showwatch.adapters.calibration import parse_calibration
from showwatch.errors import CalibrationError

BINDING = "showwatchRecord"
NAVIGATION_WAIT_MS = 5_000
ARRIVAL_WAIT_MS = 30_000
_BUTTON_INPUTS = frozenset({"button", "submit", "reset", "checkbox", "radio", "image"})
_SECRETISH = re.compile(r"token|csrf|session|auth|nonce|secret|passw|otp|key", re.IGNORECASE)
_TIME = re.compile(r"\d{1,2}[:.]\d{2}(?:\s?[AaPp]\.?[Mm]\.?)?")
_TIME_GROUP = r"(?P<time>\d{1,2}[:.]\d{2}(?: ?[AaPp]\.?[Mm]\.?)?)"
_PRICE = re.compile(r"(₹|Rs\.? ?)(\d[\d,]*)")
_HEXISH = re.compile(r"[0-9a-fA-F]{6,}")

# Runs in every page of the recording context. It reports what the user clicks or chooses; it
# changes nothing on the page. Text fields are skipped entirely.
RECORDER_JS = """
(() => {
  if (window.__showwatchRecorder) return;
  window.__showwatchRecorder = true;
  const clean = s => (s || "").replace(/\\s+/g, " ").trim();
  const textOf = el =>
    clean(el.innerText !== undefined ? el.innerText : el.textContent).slice(0, 300);
  const attrsOf = el => {
    const out = {};
    for (const a of Array.from(el.attributes)) out[a.name] = a.value.slice(0, 300);
    return out;
  };
  const levelOf = h => h.getAttribute("aria-level")
    ? Number(h.getAttribute("aria-level"))
    : (/^H[1-6]$/.test(h.tagName) ? Number(h.tagName[1]) : null);
  const headings = () => Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading]"));
  const describe = (el, kind, deep) => {
    const labelledby = (el.getAttribute("aria-labelledby") || "").split(/\\s+/).filter(Boolean)
      .map(id => document.getElementById(id)).filter(Boolean).map(textOf).join(" ");
    const info = {
      kind, url: location.href, tag: el.tagName.toLowerCase(), attrs: attrsOf(el),
      text: textOf(el), label: el.labels ? Array.from(el.labels).map(textOf).join(" ") : "",
      labelledby: clean(labelledby),
    };
    if (!deep) return info;
    let before = null;
    for (const h of headings()) {
      const after = h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING;
      if (h !== el && after) before = h;
    }
    if (before) { info.heading = textOf(before); info.heading_level = levelOf(before); }
    info.page_headings = headings().slice(0, 30).map(textOf);
    const grid = el.closest("[role=grid],[role=table],table");
    if (grid) {
      info.container = describe(grid, kind, false);
      const names = new Set();
      for (const c of Array.from(grid.querySelectorAll("*")).slice(0, 3000)) {
        for (const a of Array.from(c.attributes)) names.add(a.name);
      }
      info.cell_attributes = Array.from(names).sort().slice(0, 100);
    }
    return info;
  };
  const typing = el => {
    const type = (el.getAttribute("type") || "").toLowerCase();
    return el.tagName === "TEXTAREA" || el.isContentEditable ||
      (el.tagName === "INPUT" &&
        !["button", "submit", "reset", "checkbox", "radio", "image"].includes(type));
  };
  const pick = node => {
    const el = node instanceof Element ? node : node && node.parentElement;
    if (!el) return null;
    return el.closest("a[href],button,select,input,[role=button],[role=link],[role=option]," +
      "[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=gridcell]")
      || el.closest("[data-testid]") || el;
  };
  const report = (el, kind) => {
    if (!el || typing(el) || !window.BINDING_NAME) return;
    window.BINDING_NAME(describe(el, kind, true));
  };
  document.addEventListener("click", e => report(pick(e.target), "click"), true);
  document.addEventListener("change", e => {
    if (e.target instanceof HTMLSelectElement) report(e.target, "change");
  }, true);
})();
""".replace("BINDING_NAME", BINDING)


# --- what was recorded -----------------------------------------------------------------------


@dataclass(frozen=True)
class Interaction:
    kind: Literal["click", "change"]
    url: str  # scheme, host and path only (plus a date parameter): see safe_url()
    tag: str
    attrs: Mapping[str, str]
    text: str
    label: str = ""
    labelledby: str = ""
    heading: str | None = None  # the nearest heading before the element
    heading_level: int | None = None
    container: "Interaction | None" = None  # the grid or table it is in (seats)
    cell_attributes: tuple[str, ...] = ()  # attribute names used inside that container
    page_headings: tuple[str, ...] = ()

    @property
    def role(self) -> str | None:
        return role_of(self.tag, self.attrs)

    @property
    def name(self) -> str:
        return name_from(
            self.role,
            self.tag,
            self.attrs,
            content=self.text,
            label=self.label,
            labelledby=self.labelledby,
        )

    @classmethod
    def from_payload(cls, payload: object, *, deep: bool = True) -> "Interaction | None":
        """Build from the page script's report, dropping anything typed or token-like."""
        if not isinstance(payload, Mapping):
            return None
        kind, url, tag, attrs = (payload.get(k) for k in ("kind", "url", "tag", "attrs"))
        if kind not in ("click", "change") or not isinstance(url, str) or not isinstance(tag, str):
            return None
        if not isinstance(attrs, Mapping):
            return None
        tag = tag.lower()
        kept = {str(k): str(v) for k, v in attrs.items() if not _SECRETISH.search(str(k))}
        input_type = kept.get("type", "").lower()
        if tag == "textarea" or "contenteditable" in kept:
            return None
        if tag == "input" and input_type not in _BUTTON_INPUTS:
            return None
        if not (tag == "input" and input_type in ("button", "submit", "reset")):
            kept.pop("value", None)
        container = payload.get("container") if deep else None
        return cls(
            kind=kind,
            url=safe_url(url),
            tag=tag,
            attrs=kept,
            text=_text(payload.get("text")),
            label=_text(payload.get("label")),
            labelledby=_text(payload.get("labelledby")),
            heading=_text(payload.get("heading")) or None,
            heading_level=_level(payload.get("heading_level")),
            container=cls.from_payload(container, deep=False) if container else None,
            cell_attributes=_names(payload.get("cell_attributes"), 100),
            page_headings=_names(payload.get("page_headings"), 30),
        )


@dataclass(frozen=True)
class PageInfo:
    path_pattern: str
    title: str


@dataclass
class Recording:
    base_url: str
    hosts: tuple[str, ...] = ()
    listing_url: str | None = None
    show: Interaction | None = None
    listing_headings: tuple[str, ...] = ()
    choose_quantity: Interaction | None = None
    select_seats: Interaction | None = None
    seat: Interaction | None = None
    proceed_to_summary: Interaction | None = None
    summary: PageInfo | None = None
    hold_timer: Interaction | None = None
    continue_to_payment: Interaction | None = None
    payment: PageInfo | None = None
    navigated: dict[str, bool] = field(default_factory=dict)  # did the click load a new page?


@dataclass(frozen=True)
class Suggestion:
    spec: LocatorSpec | None
    code: str = ""
    note: str = ""


# --- suggestions (pure) ----------------------------------------------------------------------


def suggest(event: Interaction, *, navigated: bool = False) -> Suggestion:
    """A locator for the element: role and name first, then test id, then exact text."""
    role, name = event.role, event.name
    if role and name:
        if _PRICE.search(name):
            pattern = f"^{_with_prices(name)}$"
            spec = LocatorSpec(role=role, name_pattern=pattern, navigates=navigated)
            return Suggestion(spec, f'page.get_by_role({_q(role)}, name=re.compile(r"{pattern}"))')
        spec = LocatorSpec(role=role, name=name, navigates=navigated)
        return Suggestion(spec, f"page.get_by_role({_q(role)}, name={_q(name)})")
    test_id = event.attrs.get("data-testid")
    if test_id:
        spec = LocatorSpec(test_id=test_id, navigates=navigated)
        return Suggestion(spec, f"page.get_by_test_id({_q(test_id)})")
    if event.text:
        spec = LocatorSpec(text=event.text, navigates=navigated)
        return Suggestion(spec, f"page.get_by_text({_q(event.text)}, exact=True)")
    return Suggestion(None, note="nothing stable to find this element by: describe it by hand")


def suggest_seat(event: Interaction, *, navigated: bool = False) -> Suggestion:
    """A seat locator filled in with {row} and {col}, as the seat map reports them."""
    row = event.attrs.get(_attribute_named(event.attrs, ("row",)) or "")
    col = event.attrs.get(_attribute_named(event.attrs, ("col",)) or "")
    test_id = event.attrs.get("data-testid", "")
    if row and col and test_id:
        tokens = re.split(r"([^A-Za-z0-9]+)", test_id)
        if row in tokens:
            at = tokens.index(row)
            tokens[at] = "{row}"
            rest = tokens[at + 1 :]
            if col in rest:
                tokens[at + 1 + rest.index(col)] = "{col}"
                template = "".join(tokens)
                spec = LocatorSpec(test_id=template, navigates=navigated)
                return Suggestion(spec, f"page.get_by_test_id({_q(template)})")
    return Suggestion(
        None,
        note=(
            "seats must be found by row and column as the seat map gives them (for example a "
            "test id like seat-{row}-{col}); describe the seat by hand"
        ),
    )


def safe_url(url: str) -> str:
    """Scheme, host and path, plus any query parameter that holds a date. Nothing else (C7)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if _date_format(v)]
    query = "&".join(f"{k}={v}" for k, v in kept)
    return f"{parts.scheme}://{netloc}{parts.path}" + (f"?{query}" if query else "")


def url_pattern(url: str) -> str:
    """A pattern for the URL's path, with id-like segments generalised. No query, ever."""
    segments = (urlsplit(url).path or "/").split("/")
    return "^" + "/".join("[^/]+" if _id_like(s) else _literal(s) for s in segments) + "$"


def listing_url_template(url: str) -> str | None:
    """The listing URL with `{date:...}` where the date was, or None if it holds no date."""
    parts = urlsplit(url)
    found = False
    segments = []
    for segment in parts.path.split("/"):
        fmt = None if found else _date_format(segment)
        if fmt is not None:
            found = True
            segment = f"{{date:{fmt}}}"
        segments.append(segment)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        fmt = _date_format(value)
        if fmt is not None:
            found = True
            query.append(f"{key}={{date:{fmt}}}")
    if not found:
        return None
    base = f"{parts.scheme}://{parts.netloc}{'/'.join(segments)}"
    return base + (f"?{'&'.join(query)}" if query else "")


def show_name_pattern(name: str) -> str:
    """A pattern for a show link's name: its time and price become groups, the rest is literal."""
    pieces: list[str] = []
    position = 0
    spans = []
    time_match = _TIME.search(name)
    if time_match is not None:
        spans.append((time_match.start(), time_match.end(), _TIME_GROUP))
    price = _PRICE.search(name)
    if price is not None and (time_match is None or price.start() >= time_match.end()):
        group = f"{_literal(price[1])}(?P<price>[\\d,]+)"
        spans.append((price.start(), price.end(), group))
    for start, end, replacement in sorted(spans):
        pieces.append(_literal(name[position:start]))
        pieces.append(replacement)
        position = end
    pieces.append(_literal(name[position:]))
    return "".join(pieces)


def id_pattern(href: str) -> str:
    """A pattern for show links: the last path segment is the show id."""
    path = urlsplit(href).path.rstrip("/")
    prefix, _, _ = path.rpartition("/")
    return f"{_literal(prefix)}/(?P<id>[^/?#]+)"


# --- the draft -------------------------------------------------------------------------------


def draft(recording: Recording) -> dict[str, Any]:
    """The calibration as far as the recording tells. Missing parts are left out on purpose:
    validation then names them."""
    steps: dict[str, Any] = {}
    for name in ("choose_quantity", "select_seats", "proceed_to_summary"):
        event = getattr(recording, name)
        if event is not None:
            _put(steps, name, suggest(event, navigated=recording.navigated.get(name, False)))
    if recording.seat is not None:
        navigated = recording.navigated.get("seat", False)
        _put(steps, "seat", suggest_seat(recording.seat, navigated=navigated))
    forbidden: dict[str, Any] = {}
    if recording.continue_to_payment is not None:
        _put(forbidden, "continue_to_payment", suggest(recording.continue_to_payment))
    out: dict[str, Any] = {
        "version": 1,
        "site": _site(recording),
        "steps": steps,
        "forbidden": forbidden,
        "listing": _listing(recording),
    }
    seat_map = _seat_map(recording.seat)
    if seat_map:
        out["seat_map"] = seat_map
    if recording.hold_timer is not None:
        _put(out, "hold_timer", suggest(recording.hold_timer))
    pages: dict[str, Any] = {}
    for key in ("summary", "payment"):
        info: PageInfo | None = getattr(recording, key)
        if info is not None:
            pages[key] = {
                "url_patterns": [info.path_pattern],
                "title_patterns": [f"^{_literal(info.title)}$"],
            }
    if pages:
        out["pages"] = pages
    return out


def draft_yaml(recording: Recording, *, recorded_on: date) -> str:
    data = draft(recording)
    try:
        parse_calibration(data)
        todo = ["nothing: every section is filled in. Still check each entry against the site."]
    except CalibrationError as exc:
        todo = exc.problems
    lines = [
        f"# ShowWatch calibration draft, recorded {recorded_on.isoformat()} (scripts/calibrate.py)",
        "# Check each entry against the site, fill in what is missing, then save it as",
        "# calibration/bms-locators.yaml and run: uv run python scripts/calibrate.py check <file>",
        "# Calibration files are gitignored. Do not commit or share yours.",
        "#",
        "# Still to do:",
        *(f"#   - {item}" for item in todo),
    ]
    if recording.listing_headings:
        shown = ", ".join(json.dumps(h, ensure_ascii=False) for h in recording.listing_headings)
        lines += [
            "# listing.ready_pattern is text that is on the listing page once it has loaded,",
            f"#   whether or not shows are open. Headings seen there: {shown}",
        ]
    template = listing_url_template(recording.listing_url) if recording.listing_url else None
    if template:
        lines += [
            "# listing_url for the movie you recorded (each target needs its own, like this):",
            f"#   {template}",
        ]
    lines += [
        "# The listing's show pattern keeps the format you clicked as plain text. If links name",
        "#   their format, replace it with (?P<format>.+?) so every format is read.",
        "",
    ]
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(lines) + body


def _site(recording: Recording) -> dict[str, Any]:
    base_host = urlsplit(recording.base_url).hostname or ""
    site: dict[str, Any] = {"base_url": recording.base_url, "login_path": "/"}
    others = [h for h in recording.hosts if h != base_host]
    if others:
        site["hosts"] = others
    site["timezone"] = "Asia/Kolkata"
    return site


def _listing(recording: Recording) -> dict[str, Any]:
    listing: dict[str, Any] = {"tier": "auto"}
    show = recording.show
    if show is not None:
        spec: dict[str, Any] = {"role": show.role or "link"}
        if show.name:
            spec["name_pattern"] = show_name_pattern(show.name)
        href = show.attrs.get("href")
        if href:
            spec["id_pattern"] = id_pattern(href)
        listing["show"] = spec
        venue: dict[str, Any] = {"role": "heading"}
        if show.heading_level:
            venue["level"] = show.heading_level
        listing["venue"] = venue
    return listing


def _seat_map(seat: Interaction | None) -> dict[str, Any]:
    if seat is None or seat.container is None:
        return {}
    out: dict[str, Any] = {}
    container = suggest(seat.container)
    if container.spec is not None:
        out["container"] = _spec_dict(container.spec)
    for key, words in (("row_attribute", ("row",)), ("col_attribute", ("col",))):
        name = _attribute_named(seat.attrs, words)
        if name:
            out[key] = name
    names = seat.cell_attributes
    gap = next((n for n in names if re.search(r"gap|aisle|spacer|blank", n, re.IGNORECASE)), None)
    if gap:
        out["gap_attribute"] = gap
    taken = [
        n
        for n in names
        if n in ("disabled", "aria-disabled")
        or re.search(r"taken|booked|sold|unavailable|reserved|blocked", n, re.IGNORECASE)
    ]
    out["taken_attributes"] = taken or ["aria-disabled", "disabled"]  # a guess: check it
    return out


def _put(where: dict[str, Any], key: str, suggestion: Suggestion) -> None:
    if suggestion.spec is not None:
        where[key] = _spec_dict(suggestion.spec)


def _spec_dict(spec: LocatorSpec) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("role", "name", "name_pattern", "test_id", "text"):
        value = getattr(spec, key)
        if value is not None:
            out[key] = value
    if spec.navigates:
        out["navigates"] = True
    return out


def _attribute_named(attrs: Mapping[str, str], words: tuple[str, ...]) -> str | None:
    names = [n for n in attrs if any(w in n.lower() for w in words)]
    names.sort(key=lambda n: (not n.startswith("data-"), len(n)))
    return names[0] if names else None


def _with_prices(name: str) -> str:
    out, position = [], 0
    for match in _PRICE.finditer(name):
        out.append(_literal(name[position : match.start()]))
        out.append(_literal(match[1]) + r"[\d,]+")
        position = match.end()
    out.append(_literal(name[position:]))
    return "".join(out)


def _literal(text: str) -> str:
    return re.escape(text).replace("\\ ", " ")


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _id_like(segment: str) -> bool:
    return (
        any(c.isdigit() for c in segment) or bool(_HEXISH.fullmatch(segment)) or len(segment) >= 20
    )


def _date_format(value: str) -> str | None:
    for fmt, shape in (("%Y%m%d", r"\d{8}"), ("%Y-%m-%d", r"\d{4}-\d{2}-\d{2}")):
        if re.fullmatch(shape, value):
            try:
                datetime.strptime(value, fmt)  # noqa: DTZ007 (a calendar date, no time)
            except ValueError:
                return None
            return fmt
    return None


def _text(value: object) -> str:
    return normalise(value)[:300] if isinstance(value, str) else ""


def _level(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 6:
        return value
    return None


def _names(value: object, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(normalise(v)[:100] for v in value[:limit] if isinstance(v, str) and v.strip())


# --- the recording session (browser) -------------------------------------------------------------

STEPS: dict[str, str] = {
    "listing": (
        "Go to the listing page for a movie and a date (the page with cinemas and show times), "
        "then click one show time."
    ),
    "choose_quantity": (
        "Choose the number of seats in the dropdown (pick it even if it is already right). "
        "If the site has no dropdown for this, stop here with Ctrl+C: ShowWatch needs one."
    ),
    "select_seats": "Click the button that opens the seat map.",
    "seat": "Click one free seat.",
    "proceed_to_summary": (
        "Pick the rest of your seats, then click the button that goes to the order summary."
    ),
    "hold_timer": "On the order summary, click the countdown (it is harmless text).",
    "continue_to_payment": (
        "Click the button or link that leads to payment. ShowWatch will never click it. "
        "Do NOT pay on the next page."
    ),
    "done": "Recorded. Do not pay: you can close the browser now.",
}


class RecorderUI(Protocol):
    async def say(self, step: str, text: str) -> None: ...

    async def confirm(self, question: str) -> bool: ...


@dataclass
class _Captured:
    interaction: Interaction
    page: Page
    navigation: "asyncio.Future[Any]"


def _quiet(future: "asyncio.Future[Any]") -> None:
    if not future.cancelled():
        future.exception()  # retrieved, so an unneeded wait never logs an error


class RecordingSession:
    """Listens to the user's clicks in `context`. Every page gets the reporting script."""

    def __init__(self, context: BrowserContext) -> None:
        self._context = context
        self._queue: asyncio.Queue[_Captured] = asyncio.Queue()

    async def install(self) -> None:
        await self._context.expose_binding(BINDING, self._on_event)
        await self._context.add_init_script(RECORDER_JS)

    async def _on_event(self, source: Mapping[str, Any], payload: object) -> None:
        interaction = Interaction.from_payload(payload)
        page = source.get("page")
        if interaction is None or not isinstance(page, Page):
            return
        frame = page.main_frame
        # Started now, before the page can move on, so a navigation caused by this click is seen.
        navigation = asyncio.ensure_future(
            page.wait_for_event(
                "framenavigated", predicate=lambda f: f == frame, timeout=NAVIGATION_WAIT_MS
            )
        )
        navigation.add_done_callback(_quiet)
        self._queue.put_nowait(_Captured(interaction, page, navigation))

    async def next(self, accept: Callable[[Interaction], bool], timeout_s: float) -> _Captured:
        while True:
            captured = await asyncio.wait_for(self._queue.get(), timeout_s)
            if accept(captured.interaction):
                return captured
            captured.navigation.cancel()

    def drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait().navigation.cancel()


async def _navigated(captured: _Captured) -> bool:
    try:
        await captured.navigation
    except (PlaywrightError, asyncio.CancelledError):
        return False
    return True


async def _arrival(captured: _Captured) -> PageInfo:
    """The page a click led to, once its path has changed."""
    page = captured.page
    before = urlsplit(captured.interaction.url).path
    await page.wait_for_url(lambda url: urlsplit(url).path != before, timeout=ARRIVAL_WAIT_MS)
    await page.wait_for_load_state("domcontentloaded")
    return PageInfo(url_pattern(page.url), await page.title())


def _is_show_link(event: Interaction) -> bool:
    return event.role == "link" and _TIME.search(event.name) is not None


def _is_control(event: Interaction) -> bool:
    """A button or link outside the seat map (seat clicks meanwhile are not this step)."""
    return event.role in ("button", "link") and event.container is None


def _is_quantity(event: Interaction) -> bool:
    return event.tag == "select"


def _is_seat(event: Interaction) -> bool:
    return event.container is not None


def _is_plain_text(event: Interaction) -> bool:
    """The countdown: never a button or link, so a slip onto "pay" is not taken as the timer."""
    return event.role not in ("button", "link")


def _is_button_or_link(event: Interaction) -> bool:
    return event.role in ("button", "link")


# Which interaction counts as each step; anything else meanwhile is ignored.
ACCEPTS: Mapping[str, Callable[[Interaction], bool]] = {
    "listing": _is_show_link,
    "choose_quantity": _is_quantity,
    "select_seats": _is_control,
    "seat": _is_seat,
    "proceed_to_summary": _is_control,
    "hold_timer": _is_plain_text,
    "continue_to_payment": _is_button_or_link,
}


async def record_flow(
    context: BrowserContext, start_url: str, ui: RecorderUI, *, step_timeout_s: float = 1800
) -> Recording:
    """Walk the user through one booking (no payment) and record each step."""
    parts = urlsplit(start_url)
    recording = Recording(base_url=f"{parts.scheme}://{parts.netloc}")
    session = RecordingSession(context)
    await session.install()
    hosts: list[str] = []

    async def step(name: str) -> _Captured:
        await ui.say(name, STEPS[name])
        captured = await session.next(ACCEPTS[name], step_timeout_s)
        host = urlsplit(captured.interaction.url).hostname
        if host and host not in hosts:
            hosts.append(host)
        return captured

    page = await context.new_page()
    await page.goto(start_url)
    try:
        show = await step("listing")
        show.navigation.cancel()
        recording.show = show.interaction
        recording.listing_url = show.interaction.url
        recording.listing_headings = show.interaction.page_headings

        quantity = await step("choose_quantity")
        quantity.navigation.cancel()
        recording.choose_quantity = quantity.interaction

        select = await step("select_seats")
        recording.select_seats = select.interaction
        recording.navigated["select_seats"] = await _navigated(select)

        seat = await step("seat")
        recording.seat = seat.interaction
        recording.navigated["seat"] = await _navigated(seat)

        proceed = await step("proceed_to_summary")
        recording.proceed_to_summary = proceed.interaction
        recording.navigated["proceed_to_summary"] = await _navigated(proceed)
        summary = await _arrival(proceed)
        question = f"Is this the order summary? (title {summary.title!r})"
        recording.summary = summary if await ui.confirm(question) else None

        timer = await step("hold_timer")
        timer.navigation.cancel()
        recording.hold_timer = timer.interaction

        onward = await step("continue_to_payment")
        onward.navigation.cancel()
        recording.continue_to_payment = onward.interaction
        payment = await _arrival(onward)
        question = f"Is this the payment page? (title {payment.title!r})"
        recording.payment = payment if await ui.confirm(question) else None

        await ui.say("done", STEPS["done"])
    finally:
        session.drain()
    recording.hosts = tuple(hosts)
    return recording
