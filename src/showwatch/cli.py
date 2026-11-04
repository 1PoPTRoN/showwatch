"""ShowWatch command-line interface (SPEC §6).

Data lives in `$SHOWWATCH_HOME` (default `~/.showwatch`): the database, logs, run screenshots, the
saved browser profile and the run lock. Secrets come from the environment, or from a `.env` file in
the current directory (only there; parent folders are never searched).

Until the real-site adapter exists (Phase 10), `run`, `login` and `canary` need `--fakeshow URL`,
which points them at FakeShow, the local test site. `parse` and the optional locator healer use
Claude (ANTHROPIC_API_KEY, or an `ant auth login` profile).
"""

import asyncio
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, NoReturn
from zoneinfo import ZoneInfo

import anthropic
import httpx
import typer
from dotenv import load_dotenv

from showwatch import __version__, limits
from showwatch.adapters.base import SiteAdapter
from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.canary import Canary
from showwatch.clock import SystemClock
from showwatch.config import DEFAULT_TIMEZONE, Settings, Target, load_settings
from showwatch.errors import (
    AlreadyRunning,
    ConfigError,
    IllegalTransition,
    LlmError,
    NotifierConfigError,
    StoreError,
)
from showwatch.executor.browser import BrowserHolder
from showwatch.llm.client import StructuredClaude, resolve_model
from showwatch.llm.healer import Healer
from showwatch.llm.intent_parser import IntentParser, target_yaml
from showwatch.lock import InstanceLock
from showwatch.logging import configure_logging
from showwatch.notify.base import Notifier
from showwatch.notify.factory import build_notifier
from showwatch.notify.heartbeat import heartbeat_event
from showwatch.notify.messages import local_clock
from showwatch.orchestrator import Orchestrator
from showwatch.state import State, Trigger
from showwatch.store import Store

app = typer.Typer(
    name="showwatch",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks would print local variables, which could include tokens or cookies (C7).
    pretty_exceptions_show_locals=False,
)

FAKESHOW = "URL of FakeShow, the local test site (needed until the real-site adapter, Phase 10)."
FakeshowOption = Annotated[str | None, typer.Option("--fakeshow", help=FAKESHOW)]


# --- paths and seams (tests replace make_browser and interactive_login) --------------------------


def home() -> Path:
    return Path(os.environ.get("SHOWWATCH_HOME") or Path.home() / ".showwatch")


def make_llm(
    settings: Settings | None, *, timeout_s: float = 60.0, max_retries: int = 2
) -> StructuredClaude:
    """Claude for `parse` and the healer. Credentials come from the environment or `ant` profile."""
    try:
        client = anthropic.AsyncAnthropic()
    except anthropic.AnthropicError:
        _fail("Claude is not set up: put ANTHROPIC_API_KEY in .env (see .env.example).")
    return StructuredClaude(
        client, model=resolve_model(settings), timeout_s=timeout_s, max_retries=max_retries
    )


def make_browser(profile_dir: Path) -> BrowserHolder:
    return BrowserHolder(profile_dir)  # headed (C6): the user can take over at any moment


async def interactive_login(profile_dir: Path, url: str) -> None:
    """Open the site in a headed window; the user logs in by hand and closes it (C2)."""
    async with make_browser(profile_dir) as browser:
        page = await browser.new_page()
        await page.goto(url)
        typer.echo(
            "Log in yourself in the browser window, then close the window when you are done."
        )
        await page.wait_for_event("close", timeout=0)


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


@asynccontextmanager
async def _open_store() -> AsyncIterator[Store]:
    try:
        store = await Store.open(home() / "showwatch.db", clock=SystemClock())
    except StoreError as exc:
        _fail(str(exc))
    async with store:
        yield store


def _site(fakeshow: str | None) -> str:
    if fakeshow is None:
        _fail(
            "The real-site adapter arrives in Phase 10. For now, point ShowWatch at the local "
            "test site: --fakeshow http://127.0.0.1:<port>"
        )
    return fakeshow.rstrip("/")


async def _settings(store: Store) -> Settings | None:
    return await store.load_settings()


def _tz(settings: Settings | None) -> ZoneInfo:
    return settings.tz if settings else ZoneInfo(DEFAULT_TIMEZONE)


async def _notifier(store: Store, client: httpx.AsyncClient) -> Notifier:
    settings = await _settings(store)
    try:
        return build_notifier(
            settings.notifier if settings else "console",
            os.environ,
            store=store,
            client=client,
            clock=SystemClock(),
            tz=_tz(settings),
        )
    except NotifierConfigError as exc:
        _fail(str(exc))


def _lock(purpose: str) -> InstanceLock:
    lock = InstanceLock(home() / "run.lock", purpose=purpose)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        _fail(str(exc))
    return lock


# --- commands --------------------------------------------------------------------------------


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
    load_dotenv(Path.cwd() / ".env", override=False)


@app.command()
def login(fakeshow: FakeshowOption = None) -> None:
    """Open a headed browser with the saved profile so you can log in yourself."""
    site = _site(fakeshow)
    lock = _lock("showwatch login")
    try:
        asyncio.run(interactive_login(home() / "profile", f"{site}/login"))
    finally:
        lock.release()
    typer.echo("Saved. ShowWatch will reuse this login; it never sees your password.")


@app.command()
def add(
    yaml_file: Annotated[Path, typer.Argument(help="Targets YAML file to validate and add.")],
) -> None:
    """Validate a targets YAML file and add its targets."""
    asyncio.run(_add(yaml_file))


async def _add(path: Path) -> None:
    try:
        settings = await load_settings(path, clock=SystemClock())
    except ConfigError as exc:
        _fail(str(exc))
    async with _open_store() as store:
        await _check_room(store, settings.targets)
        for target in settings.targets:
            await store.add_target(target)
        await store.save_settings(settings)
    for target in settings.targets:
        typer.echo(f"Added {target.id}: {target.movie} ({target.mode.value}).")
    typer.echo("Start watching with: showwatch run")


async def _check_room(store: Store, targets: tuple[Target, ...]) -> None:
    records = await store.list_targets()
    existing = {r.target.id for r in records}
    duplicates = [t.id for t in targets if t.id in existing]
    if duplicates:
        _fail(f"a target with id {duplicates[0]!r} already exists (remove it first)")
    unfinished = sum(r.state is not State.BOOKED for r in records)
    if unfinished + len(targets) > limits.MAX_TARGETS:
        _fail(f"at most {limits.MAX_TARGETS} unfinished targets are allowed (C5)")


@app.command("list")
def list_targets() -> None:
    """List targets and their current state."""
    asyncio.run(_list())


async def _list() -> None:
    async with _open_store() as store:
        records = await store.list_targets()
    if not records:
        typer.echo("No targets yet. Add some with: showwatch add targets.yaml")
        return
    for r in records:
        typer.echo(
            f"{r.target.id:<20} {r.state.value:<17} {r.target.mode.value:<12} {r.target.movie}"
        )


@app.command()
def status(
    target_id: Annotated[str | None, typer.Argument(help="Limit to one target.")] = None,
) -> None:
    """Show detailed status for every target, or for one."""
    asyncio.run(_status(target_id))


async def _status(target_id: str | None) -> None:
    async with _open_store() as store:
        settings = await _settings(store)
        tz = _tz(settings)
        records = await store.list_targets()
        if target_id is not None:
            records = [r for r in records if r.target.id == target_id]
            if not records:
                _fail(f"no target with id {target_id!r}")
        for r in records:
            watch = await store.watch_state(r.target.id)
            attempt = await store.open_attempt(r.target.id)
            last = (
                f"last checked {local_clock(watch.last_poll_at, tz)}"
                if watch.last_poll_at
                else "not checked yet"
            )
            typer.echo(
                f"{r.target.id}: {r.target.movie} ({r.target.mode.value}), {r.state.value}, {last}"
            )
            if attempt is not None and attempt.held is not None:
                pay_by = local_clock(attempt.expires_at, tz) if attempt.expires_at else "unknown"
                typer.echo(
                    f"  attempt {attempt.attempt_id}: {', '.join(attempt.held.seats)} at "
                    f"{attempt.held.show.venue}, pay by {pay_by}"
                )
            for row in (await store.audit(r.target.id))[-5:]:
                note = f" ({row.reason})" if row.reason else ""
                typer.echo(
                    f"  {local_clock(row.at, tz)}  {row.from_state.value} -> {row.to_state.value}: "
                    f"{row.trigger.value}{note}"
                )


@app.command()
def run(
    fakeshow: FakeshowOption = None,
    max_polls: Annotated[int | None, typer.Option("--max-polls", hidden=True)] = None,
) -> None:
    """Start watching all targets (runs until stopped)."""
    site = _site(fakeshow)
    lock = _lock("showwatch run")
    undo_logging = configure_logging(home() / "logs")
    try:
        asyncio.run(_run(site, max_polls))
    except KeyboardInterrupt:
        typer.echo("Stopped.")
    finally:
        undo_logging()
        lock.release()


async def _run(site: str, max_polls: int | None) -> None:
    clock = SystemClock()
    async with _open_store() as store:
        settings = await _settings(store)
        records = await store.list_targets()
        if settings is None or not records:
            _fail("Nothing to watch yet. Add targets with: showwatch add targets.yaml")
        async with httpx.AsyncClient() as client:
            notifier = await _notifier(store, client)
            adapter = FakeSiteAdapter(site, client=client, clock=clock)
            async with make_browser(home() / "profile") as browser:
                orchestrator = Orchestrator(
                    settings=settings,
                    store=store,
                    adapter=adapter,
                    notifier=notifier,
                    clock=clock,
                    rng=random.Random(),  # noqa: S311 (poll jitter, not security)
                    open_page=browser.new_page,
                    runs_dir=home() / "runs",
                    healer=_healer(settings, adapter),
                )
                typer.echo(f"Watching {len(records)} target(s). Press Ctrl+C to stop.")
                await orchestrator.run(max_polls=max_polls)


async def _transition(target_id: str, trigger: Trigger, verb: str) -> State:
    async with _open_store() as store:
        try:
            result = await store.fire(target_id, trigger, reason=f"showwatch {verb}")
        except StoreError as exc:
            _fail(str(exc))
        except IllegalTransition:
            state = (await store.get_target(target_id)).state
            _fail(f"cannot {verb} {target_id}: it is {state.value}")
    return result.to_state


@app.command()
def pause(target_id: Annotated[str, typer.Argument(help="Target to pause.")]) -> None:
    """Pause a target: no more polling until it is resumed."""
    typer.echo(
        f"{target_id}: {asyncio.run(_transition(target_id, Trigger.USER_PAUSE, 'pause')).value}"
    )


@app.command()
def resume(target_id: Annotated[str, typer.Argument(help="Target to resume.")]) -> None:
    """Resume a paused or failed target."""
    typer.echo(
        f"{target_id}: {asyncio.run(_transition(target_id, Trigger.USER_RESUME, 'resume')).value}"
    )


async def _attempt_target(store: Store, attempt_id: str) -> str:
    try:
        attempt = await store.get_attempt(attempt_id)
    except StoreError as exc:
        _fail(str(exc))
    if attempt.closed_at is not None:
        _fail(f"attempt {attempt_id} is already finished ({attempt.outcome})")
    return attempt.target_id


@app.command()
def done(attempt_id: Annotated[str, typer.Argument(help="Attempt you paid for.")]) -> None:
    """Mark an attempt as booked after you completed payment yourself."""
    asyncio.run(_done(attempt_id))


async def _done(attempt_id: str) -> None:
    async with _open_store() as store:
        target_id = await _attempt_target(store, attempt_id)
        try:
            await store.fire(target_id, Trigger.USER_DONE, reason="showwatch done")
        except IllegalTransition:
            state = (await store.get_target(target_id)).state
            _fail(f"{target_id} is {state.value}, not waiting for payment")
    typer.echo(f"{target_id}: BOOKED. Enjoy the show!")


@app.command()
def cancel(attempt_id: Annotated[str, typer.Argument(help="Attempt to stop tracking.")]) -> None:
    """Stop tracking a held attempt (nothing is clicked on the site)."""
    asyncio.run(_cancel(attempt_id))


async def _cancel(attempt_id: str) -> None:
    async with _open_store() as store:
        target_id = await _attempt_target(store, attempt_id)
        try:
            await store.fire(target_id, Trigger.USER_CANCEL, reason="showwatch cancel")
            await store.fire(target_id, Trigger.REWATCH, reason="after cancel")
        except IllegalTransition:
            state = (await store.get_target(target_id)).state
            _fail(f"cannot cancel {attempt_id}: {target_id} is {state.value}")
    typer.echo(
        f"{target_id}: released. Nothing was clicked on the site; the hold lapses by itself."
    )


@app.command()
def remove(target_id: Annotated[str, typer.Argument(help="Target to delete.")]) -> None:
    """Delete a target and its history (frees one of the 3 target slots)."""
    asyncio.run(_remove(target_id))


async def _remove(target_id: str) -> None:
    async with _open_store() as store:
        try:
            await store.remove_target(target_id)
        except StoreError as exc:
            _fail(str(exc))
    typer.echo(f"Removed {target_id}.")


@app.command()
def heartbeat() -> None:
    """Send a summary of every target's state to the notifier."""
    asyncio.run(_heartbeat())


async def _heartbeat() -> None:
    async with _open_store() as store, httpx.AsyncClient() as client:
        notifier = await _notifier(store, client)
        settings = await _settings(store)
        await notifier.send(
            await heartbeat_event(store, SystemClock(), tz=_tz(settings), manual=True)
        )


@app.command()
def canary(fakeshow: FakeshowOption = None) -> None:
    """Read-only check that the booking flow still matches the saved locators."""
    site = _site(fakeshow)
    lock = _lock("showwatch canary")
    try:
        passed = asyncio.run(_canary(site))
    finally:
        lock.release()
    if not passed:
        raise typer.Exit(code=1)


async def _canary(site: str) -> bool:
    clock = SystemClock()
    async with _open_store() as store, httpx.AsyncClient() as client:
        records = await store.list_targets()
        if not records:
            _fail("Add a target first: the canary checks one of its shows.")
        adapter = FakeSiteAdapter(site, client=client, clock=clock)
        target = records[0].target
        snapshot = await adapter.check_availability(target)
        shows = [s for day in sorted(snapshot.open_dates) for s in snapshot.open_dates[day]]
        if not shows:
            _fail("No open show to check right now; try again when booking is open.")
        notifier = await _notifier(store, client)
        async with make_browser(home() / "profile") as browser:
            result = await Canary(
                adapter=adapter,
                notifier=notifier,
                clock=clock,
                open_page=browser.new_page,
                seats=target.seats,
                target_id=target.id,
                movie=target.movie,
            ).run(shows[0].url)
    if result.passed:
        typer.echo(
            "Canary passed: show page, seat count, seat map and the proceed button all found."
        )
    else:
        typer.echo(f"Canary failed at step {result.step}: {result.error}", err=True)
    return result.passed


@app.command()
def parse(
    text: Annotated[str, typer.Argument(help='Request in plain words, e.g. "2 IMAX tickets..."')],
) -> None:
    """Turn a plain-language request into target YAML for you to confirm."""
    asyncio.run(_parse(text))


async def _parse(text: str) -> None:
    async with _open_store() as store:
        settings = await _settings(store)
        parser = IntentParser(make_llm(settings), clock=SystemClock(), tz=_tz(settings))
        try:
            target = await parser.parse(text)
        except ConfigError as exc:
            _fail(f"Claude's draft does not pass validation, so nothing was saved.\n{exc}")
        except LlmError as exc:
            _fail(f"Could not turn that into a target: {exc}")
        typer.echo(target_yaml(target))
        typer.echo("It starts as notify-only; change `mode` in your targets file to hold seats.")
        if not typer.confirm("Add this target?", default=False):
            typer.echo("Not saved.")
            return
        await _check_room(store, (target,))
        await store.add_target(target)
        if settings is None:
            await store.save_settings(
                Settings.model_validate(
                    {
                        "version": 1,
                        "notifier": "console",
                        "targets": [target.model_dump(mode="json")],
                    }
                )
            )
    typer.echo(f"Added {target.id}. Start watching with: showwatch run")


def _healer(settings: Settings, adapter: SiteAdapter) -> Healer | None:
    if not settings.llm.healer:
        return None
    # Bounded: one request, no retries, so a broken step fails fast instead of waiting.
    return Healer(make_llm(settings, timeout_s=30.0, max_retries=0), adapter=adapter)


def main() -> None:
    app()
