"""Phase 10: reading a listing page from its HTML, by roles and accessible names (pure).

Both availability tiers use this reader: the HTTP tier on the server's HTML, the browser tier on
the rendered DOM. So the two tiers cannot disagree about what a page lists.
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from showwatch.adapters.aria import heading_level, name_from, role_of
from showwatch.adapters.calibration import Listing
from showwatch.adapters.listing import parse_document, parse_show_time, read_listing
from showwatch.errors import TransientError
from tests.support.calibration import fakeshow_calibration
from tests.support.targets import make_target

IST = ZoneInfo("Asia/Kolkata")
PAGE = "https://cinema.example.test/movie/shows?date=2026-12-18"
DAY = date(2026, 12, 18)

FAKESHOW_LIKE = """<!doctype html><html><head><title>Test Movie · FakeShow</title></head><body>
<main><h1>Test Movie</h1><h2>Choose a show</h2>
<section aria-label="Fri 18 Dec"><h3>Fri 18 Dec</h3>
  <section aria-label="Cinema A"><h4>Cinema A</h4><ul>
    <li><a href="/show/fri-a-19">7:00 PM · IMAX 2D · from ₹450</a></li>
    <li><a href="/show/fri-a-22">10:15 PM · 2D · from ₹300</a></li>
  </ul></section>
  <section aria-label="Cinema B"><h4>Cinema B</h4><ul>
    <li><a href="/show/fri-b-21">9:00 PM · 4DX · from ₹600</a></li>
  </ul></section>
</section></main>
<script>document.write("<a href='/show/ghost'>1:00 PM · 2D · from ₹1</a>")</script>
</body></html>"""


def listing(**changes: object) -> Listing:
    spec = fakeshow_calibration("https://cinema.example.test").listing
    show = spec.show.model_copy(update=changes)
    return spec.model_copy(update={"show": show})


def read(html: str, spec: Listing | None = None, **target: object) -> list[tuple[object, ...]]:
    shows = read_listing(
        parse_document(html),
        spec or listing(),
        page_url=PAGE,
        day=DAY,
        tz=IST,
        target=make_target(**target),
    )
    return [(s.show_id, s.venue, s.screen_format, s.language, s.min_price) for s in shows]


def test_shows_are_read_with_their_venue_format_price_and_url() -> None:
    assert read(FAKESHOW_LIKE) == [
        ("fri-a-19", "Cinema A", "IMAX 2D", "hindi", 450),
        ("fri-a-22", "Cinema A", "2D", "hindi", 300),
        ("fri-b-21", "Cinema B", "4DX", "hindi", 600),
    ]  # the link in the <script> is not part of the page
    (first, *_) = read_listing(
        parse_document(FAKESHOW_LIKE),
        listing(),
        page_url=PAGE,
        day=DAY,
        tz=IST,
        target=make_target(),
    )
    assert first.url == "https://cinema.example.test/show/fri-a-19"
    assert first.starts_at == datetime(2026, 12, 18, 13, 30, tzinfo=UTC)  # 7 PM in Kolkata


def test_missing_format_and_language_come_from_the_target_and_price_is_unknown() -> None:
    html = '<h4>Cinema A</h4><a href="/show/x1">7:00 PM</a>'
    spec = listing(name_pattern=r"(?P<time>\d{1,2}:\d{2} [AP]M)")
    assert read(html, spec, language="tamil", formats=["4DX", "2D"]) == [
        ("x1", "Cinema A", "4DX", "tamil", 0)
    ]


def test_without_an_id_pattern_the_link_path_is_the_show_id() -> None:
    html = '<h4>Cinema A</h4><a href="/book/42/seats?x=1">7:00 PM · 2D · from ₹1,250</a>'
    assert read(html, listing(id_pattern=None)) == [
        ("/book/42/seats", "Cinema A", "2D", "hindi", 1250)
    ]


def test_the_same_show_listed_twice_counts_once() -> None:
    html = (
        '<h4>Cinema A</h4><a href="/show/x1">7:00 PM · 2D · from ₹1</a>'
        '<a href="/show/x1">7:00 PM · 2D · from ₹2</a>'  # e.g. a second, compact layout
    )
    assert read(html) == [("x1", "Cinema A", "2D", "hindi", 1)]  # the first listing wins


def test_accessible_names_follow_aria_label_and_labelledby() -> None:
    html = (
        '<span id="v">Cinema <b>C</b></span><h4 aria-labelledby="v">ignored</h4>'
        '<a href="/show/c1" aria-label="7:00 PM · IMAX 2D · from ₹500"><span>7 PM</span></a>'
    )
    assert read(html) == [("c1", "Cinema C", "IMAX 2D", "hindi", 500)]


def test_hidden_content_is_not_part_of_a_name_or_the_page() -> None:
    html = (
        '<h4>Cinema A<span aria-hidden="true"> ★</span></h4>'
        '<a href="/show/a1">7:00 PM · 2D · from ₹1</a>'
        '<div hidden><h4>Old</h4><a href="/show/old">8:00 PM · 2D · from ₹1</a></div>'
    )
    assert read(html) == [("a1", "Cinema A", "2D", "hindi", 1)]


def test_a_show_before_any_venue_is_a_calibration_problem() -> None:
    with pytest.raises(TransientError, match=r"listing\.venue"):
        read('<a href="/show/a1">7:00 PM · 2D · from ₹1</a>')


def test_a_link_whose_href_does_not_fit_the_id_pattern_is_a_calibration_problem() -> None:
    with pytest.raises(TransientError, match="id_pattern"):
        read('<h4>Cinema A</h4><a href="/elsewhere">7:00 PM · 2D · from ₹1</a>')


def test_an_unreadable_time_is_a_calibration_problem() -> None:
    spec = listing(name_pattern=r"(?P<time>\S+) · (?P<format>.+?) · from ₹(?P<price>\d+)")
    with pytest.raises(TransientError, match="time"):
        read('<h4>Cinema A</h4><a href="/show/a1">soon · 2D · from ₹1</a>', spec)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7:00 PM", time(19, 0)),
        ("07:05 pm", time(19, 5)),
        ("12:30 AM", time(0, 30)),
        ("12:00 PM", time(12, 0)),
        ("9.45 a.m.", time(9, 45)),
        ("19:00", time(19, 0)),
    ],
)
def test_show_times(text: str, expected: time) -> None:
    assert parse_show_time(text) == expected


@pytest.mark.parametrize("text", ["25:00", "7 PM", "13:00 PM", "", "0:00 AM"])
def test_bad_show_times(text: str) -> None:
    with pytest.raises(ValueError, match="time"):
        parse_show_time(text)


def test_page_title_and_visible_text() -> None:
    doc = parse_document(FAKESHOW_LIKE)
    assert doc.title == "Test Movie · FakeShow"
    assert "Choose a show" in doc.text
    assert "document.write" not in doc.text
    assert "Test Movie · FakeShow" not in doc.text  # the title is not body text
    assert "Test Movie Choose a show" in doc.text  # blocks are separated, as a reader sees them


@pytest.mark.parametrize(
    ("tag", "attrs", "role"),
    [
        ("a", {"href": "/x"}, "link"),
        ("a", {}, None),
        ("button", {}, "button"),
        ("input", {"type": "submit"}, "button"),
        ("input", {"type": "checkbox"}, "checkbox"),
        ("input", {}, "textbox"),
        ("select", {}, "combobox"),
        ("select", {"multiple": ""}, "listbox"),
        ("h3", {}, "heading"),
        ("div", {"role": "grid"}, "grid"),
        ("div", {"role": "button link"}, "button"),
        ("span", {}, None),
    ],
)
def test_roles(tag: str, attrs: dict[str, str], role: str | None) -> None:
    assert role_of(tag, attrs) == role


def test_heading_levels() -> None:
    assert heading_level("h4", {}) == 4
    assert heading_level("div", {"role": "heading", "aria-level": "2"}) == 2
    assert heading_level("div", {}) is None


def test_names() -> None:
    assert name_from("button", "button", {}, content="  Select\n seats ") == "Select seats"
    assert name_from("button", "button", {"aria-label": "Close"}, content="x") == "Close"
    assert name_from("combobox", "select", {}, content="1 2 3", label="Number of seats") == (
        "Number of seats"
    )
    assert name_from("button", "input", {"type": "submit", "value": "Go"}, content="") == "Go"
    assert name_from("textbox", "input", {"title": "Search"}, content="") == "Search"
    assert name_from(None, "div", {}, content="text only") == ""


def test_form_controls_are_named_by_their_label() -> None:
    doc = parse_document(
        '<label for="qty">Number of seats</label><select id="qty"><option>2</option></select>'
        '<label>Seats <select id="n2"><option>1</option></select></label>'
        '<button><img alt="Pay"> now</button><br/><p>unclosed</span></p>'
    )
    controls = [e for e in doc.elements() if e.tag in ("select", "button")]
    assert [(doc.role(e), doc.name(e)) for e in controls] == [
        ("combobox", "Number of seats"),
        ("combobox", "Seats 1"),
        ("button", "Pay now"),
    ]


def test_a_venue_name_pattern_picks_venues_among_headings() -> None:
    html = (
        "<h4>Fri 18 Dec</h4><h4>Cinema A</h4>"
        '<a href="/show/a1">7:00 PM · 2D · from ₹1</a>'
        "<h4>Matinee</h4>"
        '<a href="/show/a2">9:00 PM · 2D · from ₹1</a>'
    )
    spec = fakeshow_calibration("https://cinema.example.test").listing
    venue = spec.venue.model_copy(update={"name_pattern": "^Cinema"})
    picky = spec.model_copy(update={"venue": venue})
    assert [(s[0], s[1]) for s in read(html, picky)] == [("a1", "Cinema A"), ("a2", "Cinema A")]


@pytest.mark.parametrize(
    ("tag", "attrs", "role"),
    [
        ("input", {"type": "hidden"}, None),
        ("input", {"list": "cities"}, "combobox"),
        ("img", {"alt": ""}, None),
        ("img", {"alt": "Poster"}, "img"),
        ("div", {"role": "made-up button"}, "button"),
        ("section", {"aria-label": "Cinema A"}, "region"),
        ("section", {}, None),
        ("select", {"size": "4"}, "listbox"),
    ],
)
def test_more_roles(tag: str, attrs: dict[str, str], role: str | None) -> None:
    assert role_of(tag, attrs) == role


def test_more_names() -> None:
    assert name_from("button", "input", {"type": "submit"}, content="") == "Submit"
    assert name_from("button", "input", {"type": "image", "alt": "Go"}, content="") == "Go"
    assert name_from("img", "img", {"alt": " Poster "}, content="") == "Poster"
    assert name_from("region", "section", {"title": "Info"}, content="lots of text") == "Info"
    assert name_from("button", "button", {}, content="Ok", labelledby=" Book  now ") == "Book now"


def test_only_headings_of_the_venue_level_are_venues() -> None:
    html = (
        '<h4>Cinema A</h4><h5>Audi 2</h5><a href="/show/a1">7:00 PM · 2D · from ₹1</a>'
        '<h3>Sat 19 Dec</h3><a href="/show/a2">8:00 PM · 2D · from ₹1</a>'
    )
    assert [(s[0], s[1]) for s in read(html)] == [("a1", "Cinema A"), ("a2", "Cinema A")]


def test_an_empty_show_id_is_a_calibration_problem() -> None:
    spec = listing(id_pattern=r"/show/(?P<id>[^/?#]*)")
    with pytest.raises(TransientError, match="id_pattern"):
        read('<h4>Cinema A</h4><a href="/show/">7:00 PM · 2D · from ₹1</a>', spec)
