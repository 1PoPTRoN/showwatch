"""Calibrate ShowWatch for the real booking site (Phase 10). You run this; tests and tools never do.

    uv run python scripts/calibrate.py record --site https://<the booking site>
    uv run python scripts/calibrate.py check calibration/bms-locators.yaml

`record` opens your saved ShowWatch browser profile in a visible window and walks you through one
booking: listing, seat count, seat map, order summary and the step to payment. You click; the
recorder only listens (it never clicks or types). Do not pay. It writes a draft to
calibration/bms-locators.draft.yaml (gitignored) and lists what is still to fill in.

`check` validates a calibration file and says what ShowWatch will assume from it.

Before going further than `check`, follow the manual checklist M10.1-M10.4 in README.md.
"""

import asyncio
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from playwright.async_api import async_playwright

from showwatch.adapters.calibration import load_calibration
from showwatch.cli import home
from showwatch.clock import SystemClock
from showwatch.errors import AlreadyRunning, CalibrationError
from showwatch.executor.browser import open_profile
from showwatch.lock import InstanceLock
from showwatch.recorder import draft_yaml, record_flow

DRAFT = Path("calibration") / "bms-locators.draft.yaml"

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False)


class TerminalUI:
    """Prompts in the terminal while you click in the browser."""

    def __init__(self) -> None:
        self.number = 0

    async def say(self, step: str, text: str) -> None:
        if step != "done":
            self.number += 1
            typer.echo(f"\nStep {self.number}: {text}")
        else:
            typer.echo(f"\n{text}")

    async def confirm(self, question: str) -> bool:
        return await asyncio.to_thread(typer.confirm, question, default=False)


@app.command()
def record(
    site: Annotated[str, typer.Option("--site", help="The booking site's address (https://...).")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the draft.")] = DRAFT,
) -> None:
    """Record a draft calibration while you click through one booking (no payment)."""
    parts = urlsplit(site)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        typer.echo("--site must be the site's address, like https://www.example.com", err=True)
        raise typer.Exit(code=1)
    if out.exists() and not typer.confirm(f"{out} exists. Replace it?", default=False):
        raise typer.Exit(code=1)
    lock = InstanceLock(home() / "run.lock", purpose="scripts/calibrate.py")
    try:
        lock.acquire()  # the saved profile can only be open in one place
    except AlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    try:
        typer.echo("A browser window will open. Log in there yourself if the site asks.")
        typer.echo("Never pay during calibration. Press Ctrl+C here to stop at any time.")
        text = asyncio.run(_record(site))
    finally:
        lock.release()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"\nDraft written to {out}. Open it, follow the notes at the top, then run:")
    typer.echo(f"  uv run python scripts/calibrate.py check {out}")


async def _record(site: str) -> str:
    async with async_playwright() as playwright:
        context = await open_profile(playwright, home() / "profile")  # headed (C6)
        try:
            recording = await record_flow(context, site, TerminalUI())
        finally:
            await context.close()
    return draft_yaml(recording, recorded_on=SystemClock().now().date())


@app.command("check")
def check_file(
    path: Annotated[Path, typer.Argument(help="Calibration file to validate.")],
) -> None:
    """Validate a calibration file and list what ShowWatch will assume from it."""
    try:
        calibration = load_calibration(path)
    except CalibrationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"{path} is a complete calibration for {calibration.site.base_url}.")
    for note in calibration.assumptions():
        typer.echo(f"  note: {note}")
    if path.name.endswith(".draft.yaml"):
        typer.echo("Save it as calibration/bms-locators.yaml once you have checked every entry.")


if __name__ == "__main__":
    app()
