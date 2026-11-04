"""Reading a date's listing page from HTML, by roles and accessible names (pure: no I/O).

Both availability tiers use this: the HTTP tier on the server's HTML, the browser tier on the
rendered DOM (`page.content()`). One reader means the two tiers cannot disagree.

* Shows are the elements with the calibrated role whose accessible name matches `name_pattern`.
  The pattern's `time` group is required; `format`, `language` and `price` are used when present,
  otherwise the target's first format, the target's language and price 0 (unknown).
* A show's venue is the nearest venue element before it, in document order.
* The show id comes from the link's href (`id_pattern`'s `id` group, or the href's path).
* Content in <script>, <style>, <template>, <noscript> or a `hidden` element is not on the page.
  `aria-hidden` content is left out of names, as a browser does.

A page that does not fit the calibration raises TransientError naming the calibration key, so the
user hears about a changed site instead of silently seeing "nothing open".
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from showwatch.adapters.aria import heading_level, name_from, normalise, role_of
from showwatch.adapters.calibration import Listing, Venue
from showwatch.config import Target
from showwatch.errors import TransientError
from showwatch.models import Show

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr"}
)  # fmt: skip
_INVISIBLE = frozenset({"script", "style", "template", "noscript", "head", "title"})
_BLOCK = frozenset(
    {"address", "article", "aside", "blockquote", "br", "dd", "details", "dialog", "div", "dl",
     "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
     "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "td", "th",
     "tr", "ul"}
)  # fmt: skip
_TIME = re.compile(r"(\d{1,2})[:.](\d{2})(?:\s*([AaPp])\.?\s*[Mm]\.?)?")


@dataclass(eq=False)
class Element:
    tag: str
    attrs: dict[str, str]
    parent: "Element | None"
    children: list["Element | str"] = field(default_factory=list)

    @property
    def hidden(self) -> bool:
        return "hidden" in self.attrs or self.tag in _INVISIBLE

    def content(self, *, for_name: bool = False) -> str:
        """The element's text. For a name, aria-hidden parts are left out."""
        parts: list[str] = []
        self._collect(parts, for_name=for_name)
        return normalise("".join(parts))

    def _collect(self, parts: list[str], *, for_name: bool) -> None:
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
                continue
            if child.hidden or (for_name and child.attrs.get("aria-hidden") == "true"):
                continue
            if child.tag == "img" and for_name:
                parts.append(f" {child.attrs.get('alt', '')} ")
                continue
            spaced = child.tag in _BLOCK
            if spaced:
                parts.append(" ")
            child._collect(parts, for_name=for_name)
            if spaced:
                parts.append(" ")


@dataclass
class Document:
    root: Element
    title: str
    by_id: dict[str, Element]

    @property
    def text(self) -> str:
        """The visible text of the page body (roughly what a reader sees)."""
        return self.root.content()

    def elements(self) -> Iterator[Element]:
        """Every visible element, in document order."""
        stack: list[Element] = [self.root]
        while stack:
            node = stack.pop()
            if node is not self.root:
                yield node
            stack.extend(
                child
                for child in reversed(node.children)
                if isinstance(child, Element) and not child.hidden
            )

    def role(self, element: Element) -> str | None:
        return role_of(element.tag, element.attrs)

    def name(self, element: Element) -> str:
        labelledby = " ".join(
            self.by_id[i].content(for_name=True)
            for i in element.attrs.get("aria-labelledby", "").split()
            if i in self.by_id
        )
        return name_from(
            self.role(element),
            element.tag,
            element.attrs,
            content=element.content(for_name=True),
            label=self._label(element),
            labelledby=labelledby,
        )

    def _label(self, element: Element) -> str:
        if element.tag not in ("input", "select", "textarea"):
            return ""
        own = element.attrs.get("id")
        for candidate in self.elements():
            if candidate.tag == "label" and own and candidate.attrs.get("for") == own:
                return candidate.content(for_name=True)
        node = element.parent
        while node is not None:
            if node.tag == "label":
                return node.content(for_name=True)
            node = node.parent
        return ""


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, None)
        self.current = self.root
        self.by_id: dict[str, Element] = {}
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(tag, {k: v or "" for k, v in attrs}, self.current)
        self.current.children.append(element)
        if "id" in element.attrs:
            self.by_id.setdefault(element.attrs["id"], element)
        if tag == "title":
            self._in_title = True
        if tag not in _VOID:
            self.current = element

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        node: Element | None = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:  # unmatched end tags are ignored
            self.current = node.parent

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        self.current.children.append(data)


def parse_document(html: str) -> Document:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return Document(builder.root, normalise("".join(builder.title_parts)), builder.by_id)


def parse_show_time(text: str) -> time:
    """A show time as listed: "7:00 PM", "07:05 pm", "9.45 a.m." or 24-hour "19:00"."""
    match = _TIME.fullmatch(text.strip())
    if match is None:
        raise ValueError("not a show time")
    hour, minute, half = int(match[1]), int(match[2]), (match[3] or "").lower()
    if half:
        if not 1 <= hour <= 12:
            raise ValueError("not a 12-hour show time")
        hour = hour % 12 + (12 if half == "p" else 0)
    if hour > 23 or minute > 59:
        raise ValueError("not a show time")
    return time(hour, minute)


def read_listing(
    doc: Document, listing: Listing, *, page_url: str, day: date, tz: ZoneInfo, target: Target
) -> tuple[Show, ...]:
    """The shows `doc` lists for `day` (see the module docstring)."""
    pattern = re.compile(listing.show.name_pattern)
    id_pattern = re.compile(listing.show.id_pattern) if listing.show.id_pattern else None
    venue: str | None = None
    shows: dict[str, Show] = {}
    for element in doc.elements():
        role = doc.role(element)
        if _is_venue(doc, element, role, listing.venue):
            venue = doc.name(element)
            continue
        if role != listing.show.role:
            continue
        match = pattern.search(doc.name(element))
        if match is None:
            continue
        if venue is None:
            raise TransientError(
                "a show is listed before any venue; does listing.venue still fit the site?"
            )
        show = _show(match.groupdict(), element.attrs, venue, id_pattern, page_url, day, tz, target)
        shows.setdefault(show.show_id, show)
    return tuple(shows.values())


def _is_venue(doc: Document, element: Element, role: str | None, spec: Venue) -> bool:
    if role != spec.role:
        return False
    if spec.level is not None and heading_level(element.tag, element.attrs) != spec.level:
        return False
    return spec.name_pattern is None or re.search(spec.name_pattern, doc.name(element)) is not None


def _show(
    groups: Mapping[str, str | None],
    attrs: Mapping[str, str],
    venue: str,
    id_pattern: re.Pattern[str] | None,
    page_url: str,
    day: date,
    tz: ZoneInfo,
    target: Target,
) -> Show:
    href = attrs.get("href", "")
    url = urljoin(page_url, href)
    if id_pattern is None:
        show_id = urlsplit(url).path
    else:
        found = id_pattern.search(href)
        if found is None or not found["id"]:
            raise TransientError("a show link does not fit listing.show.id_pattern")
        show_id = found["id"]
    try:
        at = parse_show_time(groups.get("time") or "")
    except ValueError:
        raise TransientError(
            "a show's time could not be read (listing.show.name_pattern)"
        ) from None
    price = (groups.get("price") or "0").replace(",", "")
    return Show(
        show_id=show_id,
        venue=venue,
        screen_format=normalise(groups.get("format") or "") or target.formats[0],
        language=normalise(groups.get("language") or "") or target.language,
        starts_at=datetime.combine(day, at, tzinfo=tz),
        min_price=int(price) if price.isdigit() else 0,
        url=url,
    )
