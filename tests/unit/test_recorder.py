"""Phase 10: the calibration recorder's suggestions (test T10.3), from synthetic interactions.

The recorder only listens while the user clicks through the site. It turns each interaction into
a suggested locator: role and accessible name first (SPEC §5), then test id, then text.
"""

import re
from datetime import date
from typing import Any

import pytest
import yaml

from showwatch.adapters.base import LocatorSpec
from showwatch.adapters.calibration import parse_calibration
from showwatch.errors import CalibrationError
from showwatch.recorder import (
    Interaction,
    PageInfo,
    Recording,
    draft_yaml,
    id_pattern,
    listing_url_template,
    show_name_pattern,
    suggest,
    suggest_seat,
    url_pattern,
)

SITE = "https://cinema.example.test"


def click(tag: str, text: str = "", **attrs: str) -> Interaction:
    return Interaction(kind="click", url=f"{SITE}/show/42", tag=tag, attrs=attrs, text=text)


def test_t10_3_a_button_click_suggests_get_by_role_with_its_name() -> None:
    suggestion = suggest(click("button", "Select seats", type="submit"))
    assert suggestion.spec == LocatorSpec(role="button", name="Select seats")
    assert suggestion.code == 'page.get_by_role("button", name="Select seats")'


def test_t10_3_the_accessible_name_prefers_aria_label() -> None:
    suggestion = suggest(click("button", "x", **{"aria-label": "Close dialog"}))
    assert suggestion.code == 'page.get_by_role("button", name="Close dialog")'


def test_t10_3_a_name_with_a_price_becomes_a_pattern() -> None:
    suggestion = suggest(click("button", "Pay ₹900"))
    assert suggestion.spec == LocatorSpec(role="button", name_pattern=r"^Pay ₹[\d,]+$")
    assert suggestion.code == r'page.get_by_role("button", name=re.compile(r"^Pay ₹[\d,]+$"))'
    assert re.fullmatch(suggestion.spec.name_pattern or "", "Pay ₹1,250")


def test_t10_3_a_select_is_found_by_its_label() -> None:
    choice = Interaction(
        kind="change", url=SITE, tag="select", attrs={"id": "qty"}, text="1 2 3", label="Seats"
    )
    assert suggest(choice).code == 'page.get_by_role("combobox", name="Seats")'


def test_t10_3_without_a_role_and_name_a_test_id_then_text_is_used() -> None:
    timer = click("p", "Complete your booking in 09:58", **{"data-testid": "hold-timer"})
    assert suggest(timer).code == 'page.get_by_test_id("hold-timer")'
    assert suggest(timer).spec == LocatorSpec(test_id="hold-timer")
    plain = click("span", "Your seats")
    assert suggest(plain).code == 'page.get_by_text("Your seats", exact=True)'


def test_t10_3_an_element_with_nothing_stable_gets_no_suggestion() -> None:
    nothing = suggest(click("div"))
    assert nothing.spec is None
    assert "by hand" in nothing.note


def test_t10_3_a_seat_test_id_is_turned_into_a_row_and_column_template() -> None:
    seat = click(
        "button",
        "H4",
        **{"data-testid": "seat-H-4", "data-row": "H", "data-col": "4", "aria-label": "H4"},
    )
    suggestion = suggest_seat(seat, navigated=True)
    assert suggestion.spec == LocatorSpec(test_id="seat-{row}-{col}", navigates=True)
    assert suggestion.code == 'page.get_by_test_id("seat-{row}-{col}")'
    by_number = click("button", "H4", **{"aria-label": "H4", "data-row": "H", "data-col": "4"})
    assert suggest_seat(by_number).spec is None  # a seat number is not a column: say so


def test_navigating_clicks_are_marked() -> None:
    assert suggest(click("button", "Select seats"), navigated=True).spec == LocatorSpec(
        role="button", name="Select seats", navigates=True
    )


# --- page and listing patterns -------------------------------------------------------------


def test_url_patterns_keep_only_the_path_and_generalise_ids() -> None:
    pattern = url_pattern(f"{SITE}/summary/ab12cd?token=s3cret#pay")
    assert pattern == "^/summary/[^/]+$"
    assert "s3cret" not in pattern
    assert re.search(pattern, "/summary/zz99")
    assert url_pattern(f"{SITE}/payment") == "^/payment$"
    assert url_pattern(f"{SITE}/summary/abcdefab") == "^/summary/[^/]+$"  # an id with no digits


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"{SITE}/movie/shows?date=2026-12-18&utm_source=x#top",
            f"{SITE}/movie/shows?date={{date:%Y-%m-%d}}",
        ),
        (
            f"{SITE}/buytickets/movie/ET001/20261218",
            f"{SITE}/buytickets/movie/ET001/{{date:%Y%m%d}}",
        ),
        (f"{SITE}/movie/no-date-here", None),
        (f"{SITE}/movie/99999999", None),  # eight digits, but not a date
    ],
)
def test_listing_url_templates(url: str, expected: str | None) -> None:
    assert listing_url_template(url) == expected


def test_show_names_become_patterns_with_named_groups() -> None:
    pattern = show_name_pattern("7:00 PM · IMAX 2D · from ₹450")
    match = re.search(pattern, "10:15 AM · IMAX 2D · from ₹1250")
    assert match is not None
    assert (match["time"], match["price"]) == ("10:15 AM", "1250")
    assert "time" in re.compile(show_name_pattern("Show at 19:00")).groupindex


def test_show_ids_come_from_the_last_path_segment() -> None:
    pattern = id_pattern("/show/fri-a-19?x=1")
    match = re.search(pattern, "/show/sat-b-21")
    assert match is not None
    assert match["id"] == "sat-b-21"


def test_text_entry_and_secrets_are_never_recorded() -> None:
    base = {"kind": "click", "url": SITE, "text": "", "label": "", "labelledby": ""}
    password = {**base, "tag": "input", "attrs": {"type": "password", "value": "hunter2"}}
    otp = {**base, "tag": "input", "attrs": {"autocomplete": "one-time-code"}}
    notes = {**base, "tag": "textarea", "attrs": {}}
    assert Interaction.from_payload(password) is None
    assert Interaction.from_payload(otp) is None
    assert Interaction.from_payload(notes) is None
    button = Interaction.from_payload(
        {**base, "tag": "input", "attrs": {"type": "submit", "value": "Go"}}
    )
    assert button is not None
    assert button.attrs["value"] == "Go"  # a button's value is its label, not user input
    link = Interaction.from_payload(
        {
            **base,
            "url": f"{SITE}/a?session=abc",
            "tag": "a",
            "attrs": {"href": "/b?t=1", "data-session-id": "abc", "data-csrf": "x", "id": "go"},
        }
    )
    assert link is not None
    assert link.url == f"{SITE}/a"  # query strings can carry tokens (C7)
    assert dict(link.attrs) == {"href": "/b?t=1", "id": "go"}  # token-like attributes dropped
    box = Interaction.from_payload(
        {**base, "tag": "input", "attrs": {"type": "checkbox", "value": "card-ending-4242"}}
    )
    assert box is not None
    assert "value" not in box.attrs  # only a button's value is its label


def test_bad_payloads_are_ignored() -> None:
    assert Interaction.from_payload("nope") is None
    assert Interaction.from_payload({"kind": "click"}) is None


# --- the draft file ------------------------------------------------------------------------


def fakeshow_recording() -> Recording:
    def event(tag: str, text: str = "", url: str = f"{SITE}/x", **attrs: Any) -> Interaction:
        return Interaction(kind="click", url=url, tag=tag, attrs=attrs, text=text)

    seat = Interaction(
        kind="click",
        url=f"{SITE}/show/fri-a-19/seats",
        tag="button",
        attrs={"data-testid": "seat-H-4", "data-row": "H", "data-col": "4", "aria-label": "H4"},
        text="H4",
        container=Interaction(
            kind="click",
            url=SITE,
            tag="div",
            attrs={"role": "grid", "aria-label": "Seat map"},
            text="",
        ),
        cell_attributes=(
            "aria-hidden",
            "data-col",
            "data-gap",
            "data-row",
            "data-taken",
            "disabled",
        ),
    )
    return Recording(
        base_url=SITE,
        hosts=("cinema.example.test",),
        listing_url=f"{SITE}/movie/shows?date=2026-12-18",
        show=Interaction(
            kind="click",
            url=f"{SITE}/movie/shows?date=2026-12-18",
            tag="a",
            attrs={"href": "/show/fri-a-19"},
            text="7:00 PM · IMAX 2D · from ₹450",
            heading="Cinema A",
            heading_level=4,
        ),
        listing_headings=("Test Movie", "Choose a show", "Fri 18 Dec", "Cinema A"),
        choose_quantity=Interaction(
            kind="change",
            url=SITE,
            tag="select",
            attrs={"id": "qty"},
            text="",
            label="Number of seats",
        ),
        select_seats=event("button", "Select seats", type="submit"),
        seat=seat,
        proceed_to_summary=event("button", "Pay ₹900", type="submit"),
        summary=PageInfo(path_pattern="^/summary/[^/]+$", title="Order summary · FakeShow"),
        hold_timer=event("p", "Complete your booking in 09:58", **{"data-testid": "hold-timer"}),
        continue_to_payment=event("a", "Continue to payment", href="/payment/ab12"),
        payment=PageInfo(path_pattern="^/payment/[^/]+$", title="Payment · FakeShow"),
        navigated={"select_seats": True, "seat": True, "proceed_to_summary": True},
    )


def test_the_draft_is_complete_except_what_only_the_user_knows() -> None:
    text = draft_yaml(fakeshow_recording(), recorded_on=date(2026, 11, 3))
    assert "Do not commit" in text or "gitignored" in text
    raw = yaml.safe_load(text)
    with pytest.raises(CalibrationError) as info:
        parse_calibration(raw)
    assert info.value.problems == ["listing.ready_pattern: required key is missing"]
    assert "Choose a show" in text  # offered as a hint for the ready pattern

    raw["listing"]["ready_pattern"] = "Choose a show"
    calibration = parse_calibration(raw)
    assert calibration.locators()["seat"] == LocatorSpec(test_id="seat-{row}-{col}", navigates=True)
    assert calibration.locators()["proceed_to_summary"] == LocatorSpec(
        role="button", name_pattern=r"^Pay ₹[\d,]+$", navigates=True
    )
    assert calibration.forbidden_locators()["continue_to_payment"] == LocatorSpec(
        role="link", name="Continue to payment"
    )
    assert calibration.seat_map.gap_attribute == "data-gap"
    assert set(calibration.seat_map.taken_attributes) == {"data-taken", "disabled"}
    assert calibration.pages.payment.title_patterns == (r"^Payment · FakeShow$",)
    assert calibration.listing.venue.level == 4


def test_a_payment_page_the_user_did_not_confirm_leaves_the_draft_invalid() -> None:
    recording = fakeshow_recording()
    recording.payment = None
    raw = yaml.safe_load(draft_yaml(recording, recorded_on=date(2026, 11, 3)))
    raw["listing"]["ready_pattern"] = "Choose a show"
    with pytest.raises(CalibrationError, match="payment"):
        parse_calibration(raw)


# --- which interaction counts as each step ---------------------------------------------------


def test_each_step_takes_only_its_kind_of_interaction() -> None:
    from showwatch.recorder import ACCEPTS

    grid = Interaction(kind="click", url=SITE, tag="div", attrs={"role": "grid"}, text="")
    show = click("a", "7:00 PM · 2D", href="/show/1")
    book = click("a", "Book tickets", href="/movie/shows")
    select = Interaction(kind="change", url=SITE, tag="select", attrs={}, text="", label="Seats")
    button = click("button", "Continue")
    seat = Interaction(kind="click", url=SITE, tag="button", attrs={}, text="H4", container=grid)
    timer = click("p", "09:58", **{"data-testid": "hold-timer"})
    pay = click("a", "Continue to payment", href="/payment/1")
    taken = {step: [e for e in (show, book, select, button, seat, timer, pay) if accept(e)]
             for step, accept in ACCEPTS.items()}  # fmt: skip
    assert taken == {
        "listing": [show],  # a link with a show time, not just any link
        "choose_quantity": [select],
        "select_seats": [show, book, button, pay],
        "seat": [seat],
        "proceed_to_summary": [show, book, button, pay],  # seat clicks meanwhile are skipped
        "hold_timer": [select, timer],  # never a button or link: a slip onto "pay" is not it
        "continue_to_payment": [show, book, button, seat, pay],
    }


async def test_a_click_counts_as_navigating_only_if_a_page_loaded() -> None:
    import asyncio
    from typing import cast

    from playwright.async_api import Page
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from showwatch.recorder import _Captured, _navigated

    loop = asyncio.get_running_loop()
    loaded, stayed = loop.create_future(), loop.create_future()
    loaded.set_result(object())
    stayed.set_exception(PlaywrightTimeoutError("no navigation"))
    page = cast(Page, object())
    event = click("button", "Continue")
    assert await _navigated(_Captured(event, page, loaded)) is True
    assert await _navigated(_Captured(event, page, stayed)) is False
