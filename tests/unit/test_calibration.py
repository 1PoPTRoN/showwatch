"""Phase 10: the calibration file format (test T10.1).

A calibration file is all BmsAdapter knows about a booking site. It must be complete, and the
payment boundary must be explicit: the payment page and at least one forbidden locator.
"""

import copy
from pathlib import Path
from typing import Any

import pytest

from showwatch.adapters.base import LocatorSpec
from showwatch.adapters.calibration import load_calibration, parse_calibration
from showwatch.errors import CalibrationError
from tests.support.calibration import fakeshow_raw

BASE = "http://127.0.0.1:8765"


def raw() -> dict[str, Any]:
    return copy.deepcopy(fakeshow_raw(BASE))


def problems(data: dict[str, Any]) -> str:
    with pytest.raises(CalibrationError) as info:
        parse_calibration(data)
    return str(info.value)


def test_t10_1_a_complete_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "bms-locators.yaml"
    path.write_text(
        Path("tests/support/fakeshow-calibration.yaml").read_text().replace("{base}", BASE)
    )
    calibration = load_calibration(path)
    assert calibration.site.base_url == BASE
    assert calibration.locators()["proceed_to_summary"] == LocatorSpec(
        role="button", name_pattern=r"^Pay ₹[\d,]+$", navigates=True
    )
    assert set(calibration.forbidden_locators()) == {"continue_to_payment", "pay_now"}


def test_t10_1_missing_steps_are_listed_by_name() -> None:
    data = raw()
    del data["steps"]["proceed_to_summary"]
    del data["steps"]["seat"]
    text = problems(data)
    assert "missing steps: proceed_to_summary, seat" in text


def test_t10_1_missing_sections_are_listed() -> None:
    data = raw()
    for section in ("seat_map", "hold_timer", "listing"):
        del data[section]
    text = problems(data)
    for section in ("seat_map", "hold_timer", "listing"):
        assert section in text


def test_the_payment_page_must_be_recognisable() -> None:
    data = raw()
    data["pages"]["payment"] = {"url_patterns": [], "title_patterns": []}
    assert "payment" in problems(data)


def test_at_least_one_forbidden_locator_is_required() -> None:
    data = raw()
    data["forbidden"] = {}
    assert "forbidden" in problems(data)


@pytest.mark.parametrize(
    ("where", "value"),
    [
        pytest.param(("listing", "show", "name_pattern"), "(unclosed", id="bad-regex"),
        pytest.param(("listing", "show", "name_pattern"), r"\d+:\d+", id="no-time-group"),
        pytest.param(("pages", "payment", "url_patterns"), ["(oops"], id="bad-page-regex"),
    ],
)
def test_patterns_must_compile_and_name_the_time(where: tuple[str, ...], value: object) -> None:
    data = raw()
    node = data
    for key in where[:-1]:
        node = node[key]
    node[where[-1]] = value
    assert where[-1] in problems(data)


def test_a_step_needs_exactly_one_way_to_find_it() -> None:
    data = raw()
    data["steps"]["select_seats"] = {"role": "button", "test_id": "x"}
    assert "select_seats" in problems(data)


@pytest.mark.parametrize("url", ["ftp://example.test", "not a url", ""])
def test_the_base_url_must_be_http(url: str) -> None:
    data = raw()
    data["site"]["base_url"] = url
    assert "base_url" in problems(data)


def test_unknown_keys_are_rejected() -> None:
    data = raw()
    data["steps"]["pay"] = {"role": "button", "name": "Pay"}  # a payment step does not exist
    data["lisitng"] = {}
    text = problems(data)
    assert "pay" in text
    assert "lisitng" in text


def _set(data: dict[str, Any], where: tuple[str, ...], value: object) -> None:
    node = data
    for key in where[:-1]:
        node = node[key]
    node[where[-1]] = value


@pytest.mark.parametrize(
    ("where", "value", "says"),
    [
        pytest.param(("seat_map", "row_attribute"), "data row", "HTML attribute", id="attribute"),
        pytest.param(("steps", "seat"), {"role": "buton"}, "not an ARIA role", id="role"),
        pytest.param(
            ("steps", "seat"), {"test_id": "x", "name": "y"}, "go with a role", id="name-alone"
        ),
        pytest.param(
            ("steps", "seat"),
            {"role": "button", "name": "a", "name_pattern": "b"},
            "not both",
            id="name-and-pattern",
        ),
        pytest.param(("steps", "seat"), {"navigates": True}, "exactly one of", id="no-way"),
        pytest.param(("site", "base_url"), "https://x.test/?a=1", "base_url", id="base-query"),
        pytest.param(("site", "login_path"), "login", "starting with /", id="login-path"),
        pytest.param(("site", "hosts"), ["https://x.test"], "plain host names", id="hosts"),
        pytest.param(("site", "timezone"), "Mars/Base", "unknown timezone", id="timezone"),
        pytest.param(
            ("listing", "show", "name_pattern"),
            "(?P<time>.+) (?P<venue>.+)",
            "groups can be",
            id="unknown-group",
        ),
        pytest.param(("listing", "show", "id_pattern"), "/show/(.+)", "(?P<id>", id="id-group"),
        pytest.param(("seat_map", "taken_attributes"), [], "taken seat", id="taken"),
        pytest.param(("forbidden",), {"Pay Now": {"role": "button"}}, "lowercase", id="key"),
        pytest.param(("listing", "venue", "level"), 7, "listing.venue.level", id="level"),
        pytest.param(("listing", "tier"), "carrier-pigeon", "listing.tier", id="tier"),
        pytest.param(("version",), 2, "version", id="version"),
    ],
)
def test_every_rule_names_where_the_problem_is(
    where: tuple[str, ...], value: object, says: str
) -> None:
    data = raw()
    _set(data, where, value)
    text = problems(data)
    assert says in text
    assert ".".join(where[:2]) in text or where[0] in text


def test_hosts_are_lowercased_and_include_the_base() -> None:
    data = raw()
    data["site"]["hosts"] = ["Pay.Example.Test"]
    hosts = parse_calibration(data).site.all_hosts
    assert hosts == frozenset({"127.0.0.1", "pay.example.test"})


def test_taken_attributes_are_kept_sorted_and_unique() -> None:
    data = raw()
    data["seat_map"]["taken_attributes"] = ["disabled", "data-taken", "disabled"]
    assert parse_calibration(data).seat_map.taken_attributes == ("data-taken", "disabled")


def test_the_top_level_must_be_a_mapping() -> None:
    with pytest.raises(CalibrationError, match="top level"):
        parse_calibration(["a", "list"])


def test_assumptions_say_what_the_file_leaves_open() -> None:
    notes = parse_calibration(raw()).assumptions()
    assert len(notes) == 1  # FakeShow's links name no language; all else is described
    assert "language" in notes[0]

    data = raw()
    data["listing"]["show"]["name_pattern"] = r"(?P<time>\S+)"
    del data["interstitials"]
    notes = parse_calibration(data).assumptions()
    assert len(notes) == 4
    for topic in ("format", "language", "price", "CAPTCHA"):
        assert any(topic in n for n in notes), topic


def test_page_rules_match_by_path_title_or_text() -> None:
    rule = parse_calibration(raw()).pages.payment
    assert rule.matches_url_or_title(path="/payment/ab", title="x")
    assert rule.matches_url_or_title(path="/x", title="Payment · FakeShow")
    assert not rule.matches_url_or_title(path="/summary/payment", title="Order summary")
    assert not rule.matches_text("anything")  # no text patterns given


# --- reading the file ----------------------------------------------------------------------


def write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "bms-locators.yaml"
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(
    ("content", "says"),
    [
        pytest.param(b"version: 1\nversion: 1\n", "duplicate key", id="duplicate-key"),
        pytest.param(b"steps: [unclosed\n", "YAML syntax error", id="syntax"),
        pytest.param(b"\xff\xfe", "UTF-8", id="not-text"),
        pytest.param(b"x: " + b"a" * 70_000, "KB", id="too-large"),
        pytest.param(b"- a\n- b\n", "top level", id="not-a-mapping"),
    ],
)
def test_unreadable_files_are_explained(tmp_path: Path, content: bytes, says: str) -> None:
    path = write(tmp_path, content)
    with pytest.raises(CalibrationError, match=says) as info:
        load_calibration(path)
    assert str(path) in str(info.value)


def test_a_missing_file_is_explained(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError, match="cannot read"):
        load_calibration(tmp_path / "nope.yaml")
