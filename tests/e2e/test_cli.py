"""Phase 8: every CLI command through CliRunner (test T8.6).

Each test gets its own SHOWWATCH_HOME and an empty working directory, so no real `.env`, database
or browser profile is ever touched. Browser launches go through a headless, locked-down holder
(C6 allows headless in tests only). The interactive login is replaced by a stub.
"""

import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer import Exit
from typer.testing import CliRunner, Result

from showwatch import cli
from showwatch.executor.browser import BrowserHolder
from showwatch.lock import InstanceLock
from tests.support.browser import LAUNCH_ARGS, lock_down
from tests.support.fakesite import FakeSite

SHOW = {
    "show_id": "s1",
    "venue": "Cinema A",
    "time": "19:00",
    "format": "IMAX 2D",
    "min_price": 450,
}


def targets_yaml(day: date, *, seats: int = 2) -> str:
    return f"""
version: 1
notifier: console
heartbeat_at: null
targets:
  - id: t1
    movie: Test Movie
    city: bengaluru
    language: hindi
    formats: [IMAX 2D]
    venues: [Cinema A]
    dates: [{day.isoformat()}]
    showtime_window: {{ start: "18:00", end: "22:30" }}
    seats: {seats}
    max_price_per_seat: 600
"""


@pytest.fixture
def sw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Result]:
    home = tmp_path / "home"
    runner = CliRunner()

    def headless_locked_browser(profile_dir: Path) -> BrowserHolder:
        return BrowserHolder(profile_dir, headless=True, args=LAUNCH_ARGS, on_context=lock_down)

    monkeypatch.setattr(cli, "make_browser", headless_locked_browser)

    def invoke(*args: str, input: str | None = None) -> Result:
        return runner.invoke(cli.app, list(args), env={"SHOWWATCH_HOME": str(home)}, input=input)

    work = tmp_path / "work"  # an empty working directory: no real .env can ever be read
    work.mkdir()
    monkeypatch.chdir(work)
    return invoke


@pytest.fixture
def soon() -> date:
    # A real future date: the CLI checks "not in the past" against the real clock.
    return datetime.now(UTC).date() + timedelta(days=30)


def test_t8_6_add_validates_and_list_status_show_the_target(
    sw: Callable[..., Result], soon: date
) -> None:
    result = sw("list")
    assert result.exit_code == 0
    assert "No targets" in result.output

    Path("bad.yaml").write_text(targets_yaml(soon, seats=11))
    result = sw("add", "bad.yaml")
    assert result.exit_code == 1
    assert "seats must be between 1 and 10" in result.output

    Path("good.yaml").write_text(targets_yaml(soon))
    result = sw("add", "good.yaml")
    assert result.exit_code == 0, result.output
    assert "t1" in result.output
    again = sw("add", "good.yaml")
    assert again.exit_code == 1
    assert "already exists" in again.output

    listing = sw("list")
    assert listing.exit_code == 0
    assert "t1" in listing.output
    assert "IDLE" in listing.output
    status = sw("status", "t1")
    assert status.exit_code == 0
    assert "not checked yet" in status.output
    assert sw("status", "nope").exit_code == 1


def test_t8_6_pause_resume_run_heartbeat_and_remove(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite
) -> None:
    Path("good.yaml").write_text(targets_yaml(soon))
    assert sw("add", "good.yaml").exit_code == 0

    paused = sw("pause", "t1")
    assert paused.exit_code == 0
    assert "PAUSED" in paused.output
    twice = sw("pause", "t1")
    assert twice.exit_code == 1  # not allowed from PAUSED
    assert "cannot" in twice.output
    resumed = sw("resume", "t1")
    assert resumed.exit_code == 0
    assert "WATCHING" in resumed.output

    fakesite.set(booking_open=False, shows=[{**SHOW, "date": soon.isoformat()}])
    run = sw("run", "--fakeshow", fakesite.base_url, "--max-polls", "1")
    assert run.exit_code == 0, run.output
    assert [e["path"] for e in fakesite.log()] == ["/movie/shows"]  # exactly one poll
    assert "last checked" in sw("status", "t1").output

    beat = sw("heartbeat")
    assert beat.exit_code == 0
    assert "t1" in beat.output
    assert "WATCHING" in beat.output

    assert sw("remove", "t1").exit_code == 0
    assert "No targets" in sw("list").output


def test_t8_6_done_and_cancel_need_a_real_attempt(sw: Callable[..., Result]) -> None:
    for command in ("done", "cancel"):
        result = sw(command, "no-such-attempt")
        assert result.exit_code == 1
        assert "no attempt" in result.output


def test_t8_6_canary_and_login_use_the_browser(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("good.yaml").write_text(targets_yaml(soon))
    assert sw("add", "good.yaml").exit_code == 0
    fakesite.set(booking_open=True, shows=[{**SHOW, "date": soon.isoformat()}])

    canary = sw("canary", "--fakeshow", fakesite.base_url)
    assert canary.exit_code == 0, canary.output
    assert "passed" in canary.output

    fakesite.set(rename_locator={"select_seats": "Choose seats"})
    broken = sw("canary", "--fakeshow", fakesite.base_url)
    assert broken.exit_code == 1
    assert "choose_quantity" in broken.output

    logins: list[str] = []

    async def fake_login(profile_dir: Path, url: str) -> None:
        logins.append(url)

    monkeypatch.setattr(cli, "interactive_login", fake_login)
    login = sw("login", "--fakeshow", fakesite.base_url)
    assert login.exit_code == 0, login.output
    assert logins == [f"{fakesite.base_url}/login"]


def test_t8_6_one_run_at_a_time(
    sw: Callable[..., Result], soon: date, tmp_path: Path, fakesite: FakeSite
) -> None:
    Path("good.yaml").write_text(targets_yaml(soon))
    assert sw("add", "good.yaml").exit_code == 0
    with InstanceLock(tmp_path / "home" / "run.lock", purpose="run"):
        for args in (
            ("run", "--fakeshow", fakesite.base_url),
            ("login", "--fakeshow", fakesite.base_url),
        ):
            result = sw(*args)
            assert result.exit_code == 1
            assert "already running" in result.output


def test_t8_6_run_without_targets(sw: Callable[..., Result], fakesite: FakeSite) -> None:
    empty = sw("run", "--fakeshow", fakesite.base_url)
    assert empty.exit_code == 1
    assert "showwatch add" in empty.output


def test_t8_6_real_site_commands_say_what_is_missing(sw: Callable[..., Result], soon: date) -> None:
    Path("good.yaml").write_text(targets_yaml(soon))
    assert sw("add", "good.yaml").exit_code == 0
    for command in ("run", "canary", "login"):
        result = sw(command)
        assert result.exit_code == 1
        assert "--fakeshow" in result.output  # the local test site is still offered
        assert "calibration/bms-locators.yaml" in result.output  # Phase 10: what is missing
        assert "scripts/calibrate.py" in result.output  # and how to make it


TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


@pytest.mark.parametrize("where", ["here", "parent"])
def test_dotenv_is_read_from_the_working_directory_only(
    where: str, tmp_path: Path, soon: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)  # never the developer's real values
    home, work = tmp_path / "home", tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    (work if where == "here" else tmp_path).joinpath(".env").write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=-1001234567890\n"
    )
    Path("t.yaml").write_text(targets_yaml(soon).replace("notifier: console", "notifier: telegram"))
    runner = CliRunner()
    env = {"SHOWWATCH_HOME": str(home)}
    try:
        assert runner.invoke(cli.app, ["add", "t.yaml"], env=env).exit_code == 0
        with respx.mock:
            route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True, "result": {}})
            )
            result = runner.invoke(cli.app, ["heartbeat"], env=env)
        if where == "here":
            assert result.exit_code == 0, result.output
            assert route.call_count == 1
        else:  # a .env further up is never picked up by accident
            assert result.exit_code == 1
            assert "TELEGRAM_BOT_TOKEN" in result.output
            assert route.call_count == 0
    finally:
        for key in ENV_KEYS:
            os.environ.pop(key, None)  # load_dotenv writes os.environ directly


# --- Phase 9: parse --------------------------------------------------------------------------


def _draft(day: date) -> dict[str, object]:
    return {
        "target_id": "parsed-1",
        "movie": "Monsoon Nights",
        "city": "bengaluru",
        "language": "hindi",
        "formats": ["IMAX 2D"],
        "venues": [],
        "dates": [day.isoformat()],
        "showtime_start": "18:00",
        "showtime_end": "22:30",
        "seats": 2,
        "preferred_rows": ["H", "J", "K"],
        "avoid_rows": [],
        "max_price_per_seat": 1000,
    }


@pytest.mark.parametrize(("answer", "saved"), [("y\n", True), ("n\n", False)])
def test_t9_1_parse_shows_yaml_and_saves_only_after_confirmation(
    answer: str,
    saved: bool,
    sw: Callable[..., Result],
    soon: date,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from showwatch.llm.client import StructuredClaude
    from tests.support.llm import FakeClaudeAPI, text_reply

    api = FakeClaudeAPI(text_reply(_draft(soon)))
    monkeypatch.setattr(
        cli, "make_llm", lambda settings: StructuredClaude(api.client(), model="claude-opus-5-5")
    )

    result = sw(
        "parse",
        "2 IMAX tickets for Monsoon Nights this Friday evening in Bengaluru, rows H to K",
        input=answer,
    )

    assert result.exit_code == 0, result.output
    assert "id: parsed-1" in result.output
    assert "mode: notify-only" in result.output
    listing = sw("list").output
    assert ("parsed-1" in listing) is saved


def test_t9_2_parse_refuses_an_invalid_answer_and_saves_nothing(
    sw: Callable[..., Result], soon: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    from showwatch.llm.client import StructuredClaude
    from tests.support.llm import FakeClaudeAPI, text_reply

    api = FakeClaudeAPI(text_reply({**_draft(soon), "seats": 40}))
    monkeypatch.setattr(
        cli, "make_llm", lambda settings: StructuredClaude(api.client(), model="claude-opus-5-5")
    )

    result = sw("parse", "40 tickets please", input="y\n")

    assert result.exit_code == 1
    assert "seats must be between 1 and 10" in result.output
    assert "No targets" in sw("list").output


# --- Phase 10: the real-site adapter, driven by a calibration file -----------------------------


def calibrated_targets_yaml(day: date, base_url: str, *, mode: str = "notify-only") -> str:
    url = f"{base_url}/movie/shows?date={{date:%Y-%m-%d}}"
    return targets_yaml(day).replace(
        "    seats: 2\n", f'    seats: 2\n    mode: {mode}\n    listing_url: "{url}"\n'
    )


def write_calibration(base_url: str, path: Path = Path("calibration/bms-locators.yaml")) -> Path:
    from tests.support.calibration import TEMPLATE

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.read_text().replace("{base}", base_url))
    return path


def test_t10_run_canary_and_login_use_the_calibration(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_calibration(fakesite.base_url)  # the default place, in the working directory
    Path("good.yaml").write_text(calibrated_targets_yaml(soon, fakesite.base_url))
    assert sw("add", "good.yaml").exit_code == 0
    fakesite.set(booking_open=True, shows=[{**SHOW, "date": soon.isoformat()}])

    run = sw("run", "--max-polls", "1")
    assert run.exit_code == 0, run.output
    assert [(e["path"], e["query"]) for e in fakesite.log()] == [
        ("/movie/shows", f"date={soon.isoformat()}")
    ]
    assert "last checked" in sw("status", "t1").output

    canary = sw("canary")
    assert canary.exit_code == 0, canary.output
    assert "passed" in canary.output

    logins: list[str] = []

    async def fake_login(profile_dir: Path, url: str) -> None:
        logins.append(url)

    monkeypatch.setattr(cli, "interactive_login", fake_login)
    login = sw("login")
    assert login.exit_code == 0, login.output
    assert logins == [f"{fakesite.base_url}/login"]


def test_t10_the_calibration_file_can_live_elsewhere(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite, tmp_path: Path
) -> None:
    elsewhere = write_calibration(fakesite.base_url, tmp_path / "site.yaml")
    Path("good.yaml").write_text(calibrated_targets_yaml(soon, fakesite.base_url))
    assert sw("add", "good.yaml").exit_code == 0
    run = sw("run", "--calibration", str(elsewhere), "--max-polls", "1")
    assert run.exit_code == 0, run.output


def test_t10_run_refuses_targets_it_cannot_watch(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite
) -> None:
    write_calibration(fakesite.base_url)
    Path("good.yaml").write_text(targets_yaml(soon))  # no listing_url
    assert sw("add", "good.yaml").exit_code == 0
    run = sw("run", "--max-polls", "1")
    assert run.exit_code == 1
    assert "t1" in run.output
    assert "listing_url" in run.output
    assert fakesite.log() == []

    assert sw("remove", "t1").exit_code == 0
    Path("other.yaml").write_text(calibrated_targets_yaml(soon, "http://203.0.113.10"))
    assert sw("add", "other.yaml").exit_code == 0
    run = sw("run", "--max-polls", "1")
    assert run.exit_code == 1
    assert "not on the calibrated site" in run.output


def test_t10_a_broken_calibration_file_lists_its_problems(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite
) -> None:
    path = write_calibration(fakesite.base_url)
    path.write_text(path.read_text().replace("  proceed_to_summary:", "  proceed_to_sumary:"))
    for command in ("run", "canary", "login"):
        result = sw(command)
        assert result.exit_code == 1
        assert "missing steps: proceed_to_summary" in result.output
        assert "did you mean 'proceed_to_summary'" in result.output


def test_t10_run_shares_one_request_gate_with_the_adapter(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A target with two dates needs two listing pages in its first check; the second is paced
    # through the same gate the watcher uses (C4). With two gates it could not be. Virtual time
    # (starting now), so the 20 s pacing does not make the test wait 20 s.
    from tests.support.clock import FakeClock

    start = datetime.now(UTC)
    monkeypatch.setattr(cli, "SystemClock", lambda: FakeClock(start))
    write_calibration(fakesite.base_url)
    later = soon + timedelta(days=1)
    yaml_text = calibrated_targets_yaml(soon, fakesite.base_url).replace(
        f"dates: [{soon.isoformat()}]", f"dates: [{soon.isoformat()}, {later.isoformat()}]"
    )
    Path("two.yaml").write_text(yaml_text)
    assert sw("add", "two.yaml").exit_code == 0
    fakesite.set(booking_open=True, shows=[{**SHOW, "date": soon.isoformat()}])
    run = sw("run", "--max-polls", "1")
    assert run.exit_code == 0, run.output
    assert [e["query"] for e in fakesite.log()] == [
        f"date={soon.isoformat()}",
        f"date={later.isoformat()}",
    ]


def test_t10_canary_says_when_the_listing_cannot_be_read(
    sw: Callable[..., Result], soon: date, fakesite: FakeSite
) -> None:
    write_calibration(fakesite.base_url)
    Path("good.yaml").write_text(calibrated_targets_yaml(soon, fakesite.base_url))
    assert sw("add", "good.yaml").exit_code == 0
    fakesite.set(booking_open=True, shows=[{**SHOW, "date": soon.isoformat()}], status_code=503)
    canary = sw("canary")
    assert canary.exit_code == 1
    assert "Could not read the listing for t1: the site answered 503" in canary.output


def test_t10_finished_targets_do_not_block_a_run() -> None:
    # A BOOKED target is never checked again, so it needs no listing_url.
    from showwatch.state import State
    from showwatch.store import TargetRecord
    from tests.support.calibration import fakeshow_calibration
    from tests.support.targets import make_target

    site = cli._Site(calibration=fakeshow_calibration("https://cinema.example.test"))
    now = datetime.now(UTC)
    booked = TargetRecord(make_target(id="old"), State.BOOKED, now, now)
    site.check_targets([booked])  # no listing_url, but finished: accepted
    watching = TargetRecord(make_target(id="new"), State.WATCHING, now, now)
    with pytest.raises(Exit):
        site.check_targets([booked, watching])
