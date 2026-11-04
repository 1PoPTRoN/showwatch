"""Phase 10: scripts/calibrate.py, run as you would run it. Only `check` and refusals are run here:
`record` opens a headed browser on a real site, which tests never do (SPEC §0)."""

import subprocess
import sys
from pathlib import Path

from tests.support.calibration import TEMPLATE

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "calibrate.py"


def calibrate(*args: str, cwd: Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (our own script, fixed arguments)
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "SHOWWATCH_HOME": str(cwd / "home"),
            # No browser can start here: if a refusal ever broke, the launch fails at once rather
            # than opening a window (C6) on the address under test.
            "PLAYWRIGHT_BROWSERS_PATH": str(cwd / "no-browsers"),
        },
        check=False,
    )


def test_check_accepts_a_complete_file_and_lists_its_assumptions(tmp_path: Path) -> None:
    good = tmp_path / "bms-locators.draft.yaml"
    good.write_text(TEMPLATE.read_text().replace("{base}", "https://cinema.example.test"))
    result = calibrate("check", str(good), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "complete calibration for https://cinema.example.test" in result.stdout
    assert "each target's language is used" in result.stdout  # FakeShow links name no language
    assert "Save it as calibration/bms-locators.yaml" in result.stdout


def test_check_lists_every_problem(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    text = TEMPLATE.read_text().replace("{base}", "https://cinema.example.test")
    bad.write_text(text.replace("  seat: {", "  seats: {"))
    result = calibrate("check", str(bad), cwd=tmp_path)
    assert result.returncode == 1
    assert "missing steps: seat" in result.stderr
    assert "steps.seats: unknown key (did you mean 'seat'?)" in result.stderr


def test_record_refuses_a_bad_address_before_opening_anything(tmp_path: Path) -> None:
    result = calibrate("record", "--site", "ftp://cinema.example.test", cwd=tmp_path)
    assert result.returncode == 1
    assert "--site must be" in result.stderr
    assert not (tmp_path / "home").exists()  # no profile, no lock: nothing was started


def test_record_keeps_an_existing_draft_unless_told(tmp_path: Path) -> None:
    draft = tmp_path / "calibration" / "bms-locators.draft.yaml"
    draft.parent.mkdir()
    draft.write_text("mine")
    result = calibrate("record", "--site", "https://cinema.example.test", cwd=tmp_path, stdin="n\n")
    assert result.returncode == 1
    assert draft.read_text() == "mine"
    assert not (tmp_path / "home").exists()
