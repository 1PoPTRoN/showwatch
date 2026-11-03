"""T0.2: `showwatch --help` exits 0 and lists every command from SPEC §6."""

import re

import pytest
from typer.testing import CliRunner

from showwatch.cli import app

SPEC_COMMANDS = (
    "login",
    "add",
    "list",
    "status",
    "run",
    "pause",
    "resume",
    "done",
    "cancel",
    "heartbeat",
    "canary",
    "parse",
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, _ANSI.sub("", result.output)


def test_t0_2_help_exits_zero() -> None:
    code, output = _invoke("--help")
    assert code == 0, output


@pytest.mark.parametrize("command", SPEC_COMMANDS)
def test_t0_2_help_lists_command(command: str) -> None:
    _, output = _invoke("--help")
    # The command must start a row of the commands table. Appearing somewhere inside
    # another command's description does not count.
    row = re.compile(rf"^[\s│|]*{re.escape(command)}\s", re.MULTILINE)
    assert row.search(output), f"{command!r} missing from --help:\n{output}"


@pytest.mark.parametrize("command", SPEC_COMMANDS)
def test_t0_2_each_command_has_working_help(command: str) -> None:
    code, output = _invoke(command, "--help")
    assert code == 0, output
