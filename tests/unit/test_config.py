"""Phase 1: loading and validating targets.yaml (SPEC §8, tests T1.1-T1.11).

Each test starts from a known-good config (`valid_raw`) and breaks exactly one thing, so a failure
points at one rule. Tests without a T-id cover YAML and pydantic traps found while designing Phase 1
(see docs/DESIGN-NOTES.md, Phase 1).
"""

import copy
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from showwatch.config import (
    Mode,
    Settings,
    Target,
    load_settings,
    parse_settings,
    parse_settings_yaml,
)
from showwatch.errors import ConfigError
from tests.support.clock import FakeClock

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "targets.example.yaml"

_TARGET: dict[str, Any] = {
    "id": "movie-fri",
    "mode": "notify-only",
    "movie": "Test Movie",
    "city": "bengaluru",
    "language": "hindi",
    "formats": ["IMAX 2D", "4DX", "2D"],
    "venues": ["Cinema A", "Cinema B"],
    "dates": ["2026-12-18", "2026-12-19"],
    "showtime_window": {"start": "18:00", "end": "22:30"},
    "seats": 2,
    "preferred_rows": ["H", "J", "K"],
    "avoid_rows": ["A", "B", "C"],
    "max_price_per_seat": 600,
}
_SETTINGS: dict[str, Any] = {
    "version": 1,
    "timezone": "Asia/Kolkata",
    "notifier": "console",
    "polling": {
        "base_interval_s": 900,
        "hot_interval_s": 60,
        "jitter_pct": 20,
        "hot_windows": [{"days": ["Mon", "Tue"], "start": "08:00", "end": "13:00"}],
    },
    "heartbeat_at": "09:00",
    "targets": [_TARGET],
}


def valid_raw() -> dict[str, Any]:
    return copy.deepcopy(_SETTINGS)


def target(raw: dict[str, Any], index: int = 0) -> dict[str, Any]:
    result: dict[str, Any] = raw["targets"][index]
    return result


def problems(raw: object, clock: FakeClock) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse_settings(raw, clock=clock)
    return info.value.problems


def error_text(raw: object, clock: FakeClock) -> str:
    return "\n".join(problems(raw, clock))


def test_valid_raw_is_valid(fake_clock: FakeClock) -> None:
    # Guards every other test: they each break exactly one thing in this config.
    assert isinstance(parse_settings(valid_raw(), clock=fake_clock), Settings)


# --- T1.1 ------------------------------------------------------------------------------------


async def test_t1_1_example_file_loads(fake_clock: FakeClock) -> None:
    settings = await load_settings(EXAMPLE, clock=fake_clock)

    assert settings.version == 1
    assert settings.notifier == "telegram"
    assert settings.heartbeat_at == time(9, 0)
    assert settings.polling.hot_windows[0].start == time(8, 0)
    (t,) = settings.targets
    assert t.id == "movie-fri"
    assert t.mode is Mode.NOTIFY_ONLY
    assert t.formats == ("IMAX 2D", "4DX", "2D")
    assert t.dates == (date(2026, 12, 18), date(2026, 12, 19))
    assert (t.showtime_window.start, t.showtime_window.end) == (time(18, 0), time(22, 30))
    assert (t.seats, t.max_price_per_seat) == (2, 600)
    assert t.preferred_rows == ("H", "J", "K")


async def test_t1_1_times_are_ist_wall_clock_times(fake_clock: FakeClock) -> None:
    settings = await load_settings(EXAMPLE, clock=fake_clock)
    window = settings.targets[0].showtime_window

    assert settings.tz == ZoneInfo("Asia/Kolkata")
    assert window.start.tzinfo is None  # wall-clock time in settings.tz, never silently UTC
    start = settings.local_datetime(date(2026, 12, 18), window.start)
    assert start.utcoffset() == timedelta(hours=5, minutes=30)
    assert start.astimezone(UTC) == datetime(2026, 12, 18, 12, 30, tzinfo=UTC)


# --- T1.2 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("seats", [0, 11])
def test_t1_2_seats_out_of_range_rejected(seats: int, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["seats"] = seats
    text = error_text(raw, fake_clock)
    assert "targets[0] (movie-fri).seats" in text
    assert "between 1 and 10" in text
    assert f"got {seats}" in text


@pytest.mark.parametrize("seats", [1, 10])
def test_t1_2_seats_at_the_limits_accepted(seats: int, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["seats"] = seats
    assert parse_settings(raw, clock=fake_clock).targets[0].seats == seats


@pytest.mark.parametrize("value", [True, "2", 2.0])
def test_numbers_must_really_be_numbers(value: object, fake_clock: FakeClock) -> None:
    # YAML `seats: yes` is True, and pydantic would otherwise read it as 1 seat.
    raw = valid_raw()
    target(raw)["seats"] = value
    assert "seats" in error_text(raw, fake_clock)


# --- T1.3 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("start", "end"), [("22:30", "18:00"), ("18:00", "18:00")])
def test_t1_3_showtime_window_must_start_before_it_ends(
    start: str, end: str, fake_clock: FakeClock
) -> None:
    raw = valid_raw()
    target(raw)["showtime_window"] = {"start": start, "end": end}
    text = error_text(raw, fake_clock)
    assert "showtime_window" in text
    assert "before" in text


# --- T1.4 ------------------------------------------------------------------------------------


def test_t1_4_empty_formats_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["formats"] = []
    text = error_text(raw, fake_clock)
    assert "formats" in text
    assert "at least one" in text


def test_t1_4_empty_venues_means_any_venue(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["venues"] = []
    assert parse_settings(raw, clock=fake_clock).targets[0].venues == ()


def test_venues_may_be_omitted(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    del target(raw)["venues"]
    assert parse_settings(raw, clock=fake_clock).targets[0].venues == ()


# --- T1.5 ------------------------------------------------------------------------------------


def test_t1_5_unknown_target_key_is_named_with_a_suggestion(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["seat"] = target(raw).pop("seats")
    text = error_text(raw, fake_clock)
    assert "targets[0] (movie-fri).seat: unknown key" in text
    assert "did you mean 'seats'?" in text


def test_t1_5_unknown_top_level_key_is_named(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["poling"] = {}
    text = error_text(raw, fake_clock)
    assert "poling: unknown key" in text
    assert "did you mean 'polling'?" in text


# --- T1.6 ------------------------------------------------------------------------------------


def test_t1_6_date_before_now_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["dates"] = ["2026-11-02", "2026-12-18"]  # FakeClock: 2026-11-03 09:00 IST
    text = error_text(raw, fake_clock)
    assert "targets[0] (movie-fri).dates" in text
    assert "2026-11-02 is in the past" in text
    assert "2026-12-18" not in text


def test_t1_6_today_is_allowed(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["dates"] = ["2026-11-03"]
    assert parse_settings(raw, clock=fake_clock).targets[0].dates == (date(2026, 11, 3),)


def test_t1_6_past_is_judged_in_the_config_timezone(fake_clock: FakeClock) -> None:
    # 2026-11-03 19:00 UTC is already 2026-11-04 00:30 in IST, so the 3rd is in the past.
    fake_clock.advance(timedelta(hours=15, minutes=30).total_seconds())
    raw = valid_raw()
    target(raw)["dates"] = ["2026-11-03"]
    assert "2026-11-03 is in the past" in error_text(raw, fake_clock)


# --- T1.7 ------------------------------------------------------------------------------------


def test_t1_7_hot_interval_below_floor_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["hot_interval_s"] = 30
    text = error_text(raw, fake_clock)
    assert "polling.hot_interval_s" in text
    assert "at least 60" in text


def test_base_interval_below_floor_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["base_interval_s"] = 59
    assert "polling.base_interval_s" in error_text(raw, fake_clock)


def test_hot_interval_may_not_exceed_base(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["hot_interval_s"] = 1000
    assert "hot_interval_s" in error_text(raw, fake_clock)


def test_hot_interval_at_floor_accepted(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["hot_interval_s"] = 60
    assert parse_settings(raw, clock=fake_clock).polling.hot_interval_s == 60


@pytest.mark.parametrize("jitter", [-1, 31])
def test_jitter_out_of_range_rejected(jitter: int, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["jitter_pct"] = jitter
    assert "jitter_pct" in error_text(raw, fake_clock)


@pytest.mark.parametrize("jitter", [0, 30])
def test_jitter_at_the_limits_accepted(jitter: int, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["jitter_pct"] = jitter
    assert parse_settings(raw, clock=fake_clock).polling.jitter_pct == jitter


# --- T1.8 ------------------------------------------------------------------------------------


def _with_targets(n: int) -> dict[str, Any]:
    raw = valid_raw()
    raw["targets"] = [{**_TARGET, "id": f"movie-{i}"} for i in range(n)]
    return raw


def test_t1_8_four_targets_rejected(fake_clock: FakeClock) -> None:
    assert "at most 3 targets" in error_text(_with_targets(4), fake_clock)


def test_three_targets_accepted(fake_clock: FakeClock) -> None:
    assert len(parse_settings(_with_targets(3), clock=fake_clock).targets) == 3


def test_zero_targets_rejected(fake_clock: FakeClock) -> None:
    assert "targets" in error_text(_with_targets(0), fake_clock)


# --- T1.9 ------------------------------------------------------------------------------------


def test_t1_9_mode_defaults_to_notify_only(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    del target(raw)["mode"]
    assert parse_settings(raw, clock=fake_clock).targets[0].mode is Mode.NOTIFY_ONLY


@pytest.mark.parametrize(
    ("value", "mode"),
    [("notify-only", Mode.NOTIFY_ONLY), ("dry-run", Mode.DRY_RUN), ("assist", Mode.ASSIST)],
)
def test_all_modes_parse(value: str, mode: Mode, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["mode"] = value
    assert parse_settings(raw, clock=fake_clock).targets[0].mode is mode


def test_unknown_mode_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["mode"] = "autopay"
    assert "mode" in error_text(raw, fake_clock)


# --- T1.10 -----------------------------------------------------------------------------------

SECRET = "123456:AAH-very-secret-token"  # a fake token the tests must never see echoed


@pytest.mark.parametrize(
    ("place", "key", "env_var"),
    [
        pytest.param("top", "telegram_token", "TELEGRAM_BOT_TOKEN", id="telegram_token"),
        pytest.param("top", "bot_token", "TELEGRAM_BOT_TOKEN", id="bot_token"),
        pytest.param("top", "telegram_chat_id", "TELEGRAM_CHAT_ID", id="chat_id"),
        pytest.param("target", "anthropic_api_key", "ANTHROPIC_API_KEY", id="nested-api-key"),
        pytest.param("polling", "password", ".env", id="nested-password"),
    ],
)
def test_t1_10_secrets_in_yaml_rejected_and_pointed_to_env(
    place: str, key: str, env_var: str, fake_clock: FakeClock
) -> None:
    raw = valid_raw()
    {"top": raw, "target": target(raw), "polling": raw["polling"]}[place][key] = SECRET
    with pytest.raises(ConfigError) as info:
        parse_settings(raw, clock=fake_clock)
    text = str(info.value)
    assert key in text
    assert ".env" in text
    assert env_var in text
    assert SECRET not in text  # C7: the secret itself is never echoed back


def test_error_messages_never_carry_input_values(fake_clock: FakeClock) -> None:
    # A typo'd key holding a token is "unknown", not a recognised secret. Its value must still not
    # leak through the message or a chained pydantic error, whose text includes input values.
    raw = valid_raw()
    raw["telgram_tokn"] = SECRET
    with pytest.raises(ConfigError) as info:
        parse_settings(raw, clock=fake_clock)
    assert SECRET not in str(info.value)
    assert SECRET not in repr(info.value.problems)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


# --- T1.11 -----------------------------------------------------------------------------------


def test_t1_11_duplicate_target_ids_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["targets"] = [_TARGET, {**_TARGET, "movie": "Another"}]
    assert "duplicate target id 'movie-fri'" in error_text(raw, fake_clock)


# --- times: YAML reads unquoted 18:00 as the integer 1080 --------------------------------------


def test_unquoted_time_rejected_with_a_quoting_hint(fake_clock: FakeClock) -> None:
    text = """
version: 1
targets:
  - id: movie-fri
    movie: Test Movie
    city: bengaluru
    language: hindi
    formats: [2D]
    dates: [2026-12-18]
    showtime_window: { start: 18:00, end: "22:30" }
    seats: 2
    max_price_per_seat: 600
"""
    with pytest.raises(ConfigError) as info:
        parse_settings_yaml(text, clock=fake_clock)
    message = str(info.value)
    assert "showtime_window.start" in message
    assert 'quote it: "18:00"' in message


@pytest.mark.parametrize("value", ["6pm", "25:00", "18:60", "1800", ""])
def test_malformed_time_rejected(value: str, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["showtime_window"] = {"start": value, "end": "22:30"}
    assert "HH:MM" in error_text(raw, fake_clock)


def test_single_digit_hour_accepted(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["showtime_window"] = {"start": "9:05", "end": "22:30"}
    assert parse_settings(raw, clock=fake_clock).targets[0].showtime_window.start == time(9, 5)


# --- YAML hygiene ------------------------------------------------------------------------------


def test_duplicate_yaml_keys_rejected(fake_clock: FakeClock) -> None:
    # Plain PyYAML silently keeps the last value.
    with pytest.raises(ConfigError, match=r"duplicate key 'seats'.*line 4"):
        parse_settings_yaml("version: 1\ntargets:\n  - seats: 2\n    seats: 9\n", clock=fake_clock)


def test_yaml_syntax_error_reports_the_line(fake_clock: FakeClock) -> None:
    with pytest.raises(ConfigError, match="line 2"):
        parse_settings_yaml("version: 1\ntargets: [unclosed\n", clock=fake_clock)


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n"])
def test_top_level_must_be_a_mapping(text: str, fake_clock: FakeClock) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        parse_settings_yaml(text, clock=fake_clock)


async def test_oversized_file_rejected(tmp_path: Path, fake_clock: FakeClock) -> None:
    big = tmp_path / "targets.yaml"
    big.write_text("# padding\n" * 10_000)
    with pytest.raises(ConfigError, match="too large"):
        await load_settings(big, clock=fake_clock)


async def test_missing_file_is_a_config_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        await load_settings(tmp_path / "nope.yaml", clock=fake_clock)


# --- rows, labels and normalisation ------------------------------------------------------------


def test_row_labels_are_upper_cased_and_numbers_accepted(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["preferred_rows"] = ["h", "j"]
    target(raw)["avoid_rows"] = [1, 2]
    t = parse_settings(raw, clock=fake_clock).targets[0]
    assert t.preferred_rows == ("H", "J")
    assert t.avoid_rows == ("1", "2")


def test_boolean_row_label_rejected(fake_clock: FakeClock) -> None:
    # YAML reads an unquoted `ON` as True.
    raw = valid_raw()
    target(raw)["avoid_rows"] = [True]
    assert "avoid_rows" in error_text(raw, fake_clock)


def test_row_both_preferred_and_avoided_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["avoid_rows"] = ["A", "h"]
    assert "row 'H' is in both preferred_rows and avoid_rows" in error_text(raw, fake_clock)


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("formats", ["IMAX 2D", "imax  2d"]),
        ("venues", ["Cinema A", "cinema a"]),
        ("dates", ["2026-12-18", "2026-12-18"]),
        ("preferred_rows", ["H", "h"]),
    ],
)
def test_duplicates_rejected(field: str, values: list[str], fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)[field] = values
    assert "duplicate" in error_text(raw, fake_clock)


def test_numeric_movie_title_kept_as_text(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["movie"] = 1917
    assert parse_settings(raw, clock=fake_clock).targets[0].movie == "1917"


def test_dates_sorted(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["dates"] = ["2026-12-19", "2026-12-18"]
    t = parse_settings(raw, clock=fake_clock).targets[0]
    assert t.dates == (date(2026, 12, 18), date(2026, 12, 19))


@pytest.mark.parametrize("value", [20261218, "18/12/2026", "2026-12-18T18:00:00"])
def test_ambiguous_dates_rejected(value: object, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["dates"] = [value]
    assert "YYYY-MM-DD" in error_text(raw, fake_clock)


# --- other fields ------------------------------------------------------------------------------


@pytest.mark.parametrize("target_id", ["Movie Fri", "../escape", "", "-lead", "a" * 41])
def test_target_id_must_be_a_short_slug(target_id: str, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["id"] = target_id
    assert "id" in error_text(raw, fake_clock)


def test_unknown_timezone_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["timezone"] = "Mars/Olympus_Mons"
    assert "timezone" in error_text(raw, fake_clock)


@pytest.mark.parametrize("version", [None, 2, True])
def test_version_must_be_1(version: object, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    if version is None:
        del raw["version"]
    else:
        raw["version"] = version
    assert "version" in error_text(raw, fake_clock)


def test_unknown_notifier_rejected(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["notifier"] = "sms"
    assert "notifier" in error_text(raw, fake_clock)


@pytest.mark.parametrize("price", [0, -100])
def test_price_must_be_positive(price: int, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["max_price_per_seat"] = price
    assert "max_price_per_seat" in error_text(raw, fake_clock)


def test_hot_window_must_start_before_it_ends(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["hot_windows"] = [{"days": ["Mon"], "start": "13:00", "end": "08:00"}]
    assert "hot_windows" in error_text(raw, fake_clock)


def test_hot_window_day_names_normalised_and_checked(fake_clock: FakeClock) -> None:
    raw = valid_raw()
    raw["polling"]["hot_windows"] = [{"days": ["monday", "SUN"], "start": "08:00", "end": "13:00"}]
    window = parse_settings(raw, clock=fake_clock).polling.hot_windows[0]
    assert window.days == ("Mon", "Sun")

    raw["polling"]["hot_windows"][0]["days"] = ["Funday"]
    assert "days" in error_text(raw, fake_clock)


def test_all_problems_reported_at_once(fake_clock: FakeClock) -> None:
    # The past-date check runs even when the structure is invalid: users fix everything in one go.
    raw = valid_raw()
    target(raw)["seats"] = 0
    target(raw)["formats"] = []
    target(raw)["dates"] = ["2026-11-01"]
    raw["polling"]["hot_interval_s"] = 30
    found = problems(raw, fake_clock)
    assert len(found) == 4, found


def test_target_with_passed_dates_still_constructs() -> None:
    # The past-date rule applies to user input only. A stored target whose dates have since passed
    # must still load after a restart; stopping it is the orchestrator's job (DESIGN-NOTES P8.2).
    raw = copy.deepcopy(_TARGET)
    raw["dates"] = ["2020-01-01"]
    assert Target.model_validate(raw).dates == (date(2020, 1, 1),)


def test_settings_are_immutable(fake_clock: FakeClock) -> None:
    t = parse_settings(valid_raw(), clock=fake_clock).targets[0]
    with pytest.raises(ValidationError):
        t.seats = 9  # type: ignore[misc]


# --- Phase 10: where to look for a target's shows on the real site ---------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://cinema.example.test/buytickets/test-movie/ET001/{date:%Y%m%d}",
        "https://cinema.example.test/movie/shows?date={date}",
        "http://127.0.0.1:8000/movie/shows?date={date:%Y-%m-%d}&lang=hi",
    ],
)
def test_listing_url_accepts_a_date_placeholder(url: str, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["listing_url"] = url
    assert parse_settings(raw, clock=fake_clock).targets[0].listing_url == url


def test_listing_url_is_optional(fake_clock: FakeClock) -> None:
    assert parse_settings(valid_raw(), clock=fake_clock).targets[0].listing_url is None


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://cinema.example.test/movie/shows", id="no-date"),
        pytest.param("https://cinema.example.test/{city}/{date}", id="other-field"),
        pytest.param("https://cinema.example.test/{date!r}", id="conversion"),
        pytest.param("https://cinema.example.test/{date:%Q}x{", id="broken-braces"),
        pytest.param("ftp://cinema.example.test/{date}", id="not-http"),
        pytest.param("/movie/shows/{date}", id="no-host"),
    ],
)
def test_listing_url_must_be_web_address_with_the_date(url: str, fake_clock: FakeClock) -> None:
    raw = valid_raw()
    target(raw)["listing_url"] = url
    text = error_text(raw, fake_clock)
    assert "targets[0] (movie-fri).listing_url" in text
    assert "{date" in text  # the message shows how to write it
    assert "cinema.example.test" not in text  # but never echoes the input (C7)
