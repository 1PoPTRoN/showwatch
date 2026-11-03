"""ShowWatch command-line interface (SPEC §6).

Phase 0: every command is a stub, so `--help` already documents the full command set. Each stub
prints which phase implements it and exits with status 1.
"""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from showwatch import __version__

app = typer.Typer(
    name="showwatch",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks would print local variables, which could include tokens or cookies (C7).
    pretty_exceptions_show_locals=False,
)


def _not_implemented(command: str, phase: int) -> NoReturn:
    typer.echo(f"showwatch {command}: not implemented yet (arrives in Phase {phase}).", err=True)
    raise typer.Exit(code=1)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"showwatch {__version__}")
        raise typer.Exit


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """Watch for movie booking openings and hold good seats. Payment always stays manual."""


@app.command()
def login() -> None:
    """Open a headed browser with the saved profile so you can log in yourself."""
    _not_implemented("login", phase=8)


@app.command()
def add(
    yaml_file: Annotated[Path, typer.Argument(help="Targets YAML file to validate and add.")],
) -> None:
    """Validate a targets YAML file and add its targets."""
    _not_implemented("add", phase=8)


@app.command("list")
def list_targets() -> None:
    """List targets and their current state."""
    _not_implemented("list", phase=8)


@app.command()
def status(
    target_id: Annotated[str | None, typer.Argument(help="Limit to one target.")] = None,
) -> None:
    """Show detailed status for every target, or for one."""
    _not_implemented("status", phase=8)


@app.command()
def run() -> None:
    """Start watching all targets (runs until stopped)."""
    _not_implemented("run", phase=8)


@app.command()
def pause(target_id: Annotated[str, typer.Argument(help="Target to pause.")]) -> None:
    """Pause a target: no more polling until it is resumed."""
    _not_implemented("pause", phase=8)


@app.command()
def resume(target_id: Annotated[str, typer.Argument(help="Target to resume.")]) -> None:
    """Resume a paused or failed target."""
    _not_implemented("resume", phase=8)


@app.command()
def done(attempt_id: Annotated[str, typer.Argument(help="Attempt you paid for.")]) -> None:
    """Mark an attempt as booked after you completed payment yourself."""
    _not_implemented("done", phase=8)


@app.command()
def cancel(attempt_id: Annotated[str, typer.Argument(help="Attempt to stop tracking.")]) -> None:
    """Stop tracking a held attempt (nothing is clicked on the site)."""
    _not_implemented("cancel", phase=8)


@app.command()
def heartbeat() -> None:
    """Send a summary of every target's state to the notifier."""
    _not_implemented("heartbeat", phase=8)


@app.command()
def canary() -> None:
    """Read-only check that the booking flow still matches the saved locators."""
    _not_implemented("canary", phase=8)


@app.command()
def parse(
    text: Annotated[str, typer.Argument(help='Request in plain words, e.g. "2 IMAX tickets..."')],
) -> None:
    """Turn a plain-language request into target YAML for you to confirm."""
    _not_implemented("parse", phase=9)


def main() -> None:
    app()
