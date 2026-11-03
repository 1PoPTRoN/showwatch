"""Phase 5: Telegram and console notifiers, delivery dedup and the heartbeat (tests T5.1-T5.7).

The Telegram Bot API is mocked with respx: no request reaches the network.
"""

import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from showwatch import logging as swlog
from showwatch.errors import NotificationFailed, NotifierConfigError
from showwatch.models import Event, EventKind, Snapshot
from showwatch.notify.console import ConsoleNotifier
from showwatch.notify.dedup import DedupNotifier
from showwatch.notify.heartbeat import heartbeat_event
from showwatch.notify.messages import render
from showwatch.notify.telegram import TelegramConfig, TelegramNotifier
from showwatch.state import Trigger
from showwatch.store import Store
from tests.support.clock import FakeClock
from tests.support.notify import RecordingNotifier
from tests.support.shows import FRI, show
from tests.support.targets import make_target

IST = ZoneInfo("Asia/Kolkata")
TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
CHAT = "-1001234567890"
API = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
OK = httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


@pytest.fixture
def redaction() -> Iterator[None]:
    uninstall = swlog.install_redaction()
    try:
        yield
    finally:
        uninstall()
        swlog.forget_secrets()


def telegram(client: httpx.AsyncClient, clock: FakeClock) -> TelegramNotifier:
    return TelegramNotifier(TelegramConfig(TOKEN, CHAT), client=client, clock=clock, tz=IST)


def sample(clock: FakeClock, event_id: str = "movie-fri-abc:held") -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.HELD,
        at=clock.now(),
        target_id="movie-fri",
        movie="Fast & Furious",
        attempt_id="movie-fri-abc",
        show=show("s1"),
        seats=("H9", "H10"),
        expires_at=clock.now() + timedelta(minutes=8),
    )


# --- T5.1 ------------------------------------------------------------------------------------


@respx.mock
async def test_t5_1_sends_html_to_the_configured_chat(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    route = respx.post(API).mock(return_value=OK)
    event = sample(fake_clock)

    await telegram(client, fake_clock).send(event)

    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["chat_id"] == CHAT
    assert body["parse_mode"] == "HTML"
    assert body["text"] == render(event, tz=IST, html=True)
    assert "Fast &amp; Furious" in body["text"]
    assert body["disable_web_page_preview"] is True


# --- T5.3 ------------------------------------------------------------------------------------


@respx.mock
async def test_t5_3_server_errors_are_retried_three_times_then_logged(
    client: httpx.AsyncClient, fake_clock: FakeClock, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx.post(API).mock(return_value=httpx.Response(502))
    notifier = DedupNotifier(telegram(client, fake_clock), store=store)

    with caplog.at_level(logging.ERROR, logger="showwatch.notify"):
        await notifier.send(sample(fake_clock))  # never raises: the pipeline continues

    assert route.call_count == 4  # the first try and 3 retries
    assert fake_clock.sleeps == [1, 2, 4]  # exponential backoff, on the injected clock
    assert any("could not deliver" in r.getMessage() for r in caplog.records)
    assert not await store.notification_sent("movie-fri-abc:held")  # can be delivered later


@respx.mock
async def test_telegram_429_waits_the_requested_time(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    slow_down = httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}})
    route = respx.post(API).mock(side_effect=[slow_down, OK])
    await telegram(client, fake_clock).send(sample(fake_clock))
    assert route.call_count == 2
    assert fake_clock.sleeps == [7]


@respx.mock
async def test_network_errors_are_retried(client: httpx.AsyncClient, fake_clock: FakeClock) -> None:
    route = respx.post(API).mock(side_effect=[httpx.ConnectError("down"), OK])
    await telegram(client, fake_clock).send(sample(fake_clock))
    assert route.call_count == 2


@respx.mock
async def test_client_errors_are_not_retried(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    route = respx.post(API).mock(
        return_value=httpx.Response(
            400, json={"ok": False, "description": "Bad Request: chat not found"}
        )
    )
    with pytest.raises(NotificationFailed, match="chat not found"):
        await telegram(client, fake_clock).send(sample(fake_clock))
    assert route.call_count == 1


# --- T5.4 ------------------------------------------------------------------------------------


async def test_t5_4_the_same_event_is_delivered_once(
    store: Store, fake_clock: FakeClock, tmp_path: Path
) -> None:
    inner = RecordingNotifier()
    await DedupNotifier(inner, store=store).send(sample(fake_clock))
    await DedupNotifier(inner, store=store).send(sample(fake_clock))
    assert len(inner.events) == 1

    await store.close()  # the record survives a restart
    async with await Store.open(tmp_path / "showwatch.db", clock=fake_clock) as reopened:
        await DedupNotifier(inner, store=reopened).send(sample(fake_clock))
    assert len(inner.events) == 1


async def test_a_failed_delivery_is_tried_again_next_time(
    store: Store, fake_clock: FakeClock
) -> None:
    inner = RecordingNotifier(fail=True)
    notifier = DedupNotifier(inner, store=store)
    await notifier.send(sample(fake_clock))
    inner.fail = False
    await notifier.send(sample(fake_clock))
    assert len(inner.events) == 2
    assert await store.notification_sent("movie-fri-abc:held")


# --- T5.5 ------------------------------------------------------------------------------------


@respx.mock
async def test_t5_5_the_bot_token_never_reaches_a_log_record(
    redaction: None,
    client: httpx.AsyncClient,
    fake_clock: FakeClock,
    store: Store,
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(API).mock(
        side_effect=[
            httpx.Response(500),
            httpx.ConnectError(f"cannot reach {API}"),
            httpx.Response(503),
            OK,
        ]
    )
    caplog.set_level(logging.DEBUG)  # every logger, httpx and httpcore included
    for name in ("httpx", "httpcore", "showwatch"):
        caplog.set_level(logging.DEBUG, logger=name)  # restored after the test

    await DedupNotifier(telegram(client, fake_clock), store=store).send(sample(fake_clock))
    respx.post(API).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": f"bad {TOKEN}"})
    )
    await DedupNotifier(telegram(client, fake_clock), store=store).send(sample(fake_clock, "other"))

    # The test must actually see httpx logging the token-bearing URL, or it proves nothing.
    assert any(
        r.name.startswith("httpx") and "api.telegram.org" in r.getMessage() for r in caplog.records
    )
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in str(record.msg)
        assert TOKEN not in str(record.args)
        assert TOKEN not in (record.exc_text or "")
    assert TOKEN not in caplog.text


# --- T5.6 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        ({}, ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]),
        ({"TELEGRAM_BOT_TOKEN": TOKEN}, ["TELEGRAM_CHAT_ID"]),
        ({"TELEGRAM_CHAT_ID": CHAT, "TELEGRAM_BOT_TOKEN": "  "}, ["TELEGRAM_BOT_TOKEN"]),
    ],
)
def test_t5_6_missing_env_vars_are_a_clear_startup_error(
    environ: dict[str, str], missing: list[str]
) -> None:
    with pytest.raises(NotifierConfigError) as info:
        TelegramConfig.from_env(environ)
    message = str(info.value)
    for name in missing:
        assert name in message
    assert ".env" in message


def test_malformed_token_is_rejected_without_echoing_it() -> None:
    bad = "not-a-real-token-but-secret"
    with pytest.raises(NotifierConfigError, match="TELEGRAM_BOT_TOKEN") as info:
        TelegramConfig.from_env({"TELEGRAM_BOT_TOKEN": bad, "TELEGRAM_CHAT_ID": CHAT})
    assert bad not in str(info.value)


def test_config_from_env_trims_and_registers_the_token(redaction: None) -> None:
    config = TelegramConfig.from_env({"TELEGRAM_BOT_TOKEN": f" {TOKEN} ", "TELEGRAM_CHAT_ID": CHAT})
    assert config.token == TOKEN
    assert repr(config).count(TOKEN) == 0  # never printed by accident
    assert TOKEN not in swlog.redact(f"x {TOKEN} y")


async def test_t5_6_console_notifier_works_without_any_env(fake_clock: FakeClock) -> None:
    stream = io.StringIO()
    await ConsoleNotifier(stream=stream, tz=IST).send(sample(fake_clock))
    output = stream.getvalue()
    assert "Fast & Furious" in output  # plain text, not HTML
    assert "H9, H10" in output
    assert output.startswith("[ShowWatch 9:00 AM]")


# --- T5.7 ------------------------------------------------------------------------------------


async def test_t5_7_heartbeat_lists_every_target_with_state_and_last_poll(
    store: Store, fake_clock: FakeClock
) -> None:
    await store.add_target(make_target(id="watching", movie="Movie One"))
    await store.fire("watching", Trigger.START)
    await store.add_target(make_target(id="paused", movie="Movie Two"))
    await store.fire("paused", Trigger.USER_PAUSE)
    await store.add_target(make_target(id="fresh", movie="Movie Three"))
    fake_clock.advance(60)
    seen = Snapshot("watching", fake_clock.now(), {FRI: (show("s1"),)})
    await store.record_poll("watching", snapshot=seen, seen=seen, changes=[])
    fake_clock.advance(120)

    event = await heartbeat_event(store, fake_clock)

    assert event.kind is EventKind.HEARTBEAT
    assert [(s.target_id, s.state) for s in event.statuses] == [
        ("watching", "WATCHING"),
        ("paused", "PAUSED"),
        ("fresh", "IDLE"),
    ]
    assert event.statuses[0].last_poll_at == fake_clock.now() - timedelta(seconds=120)
    assert event.statuses[1].last_poll_at is None
    text = render(event, tz=IST, html=False)
    for movie in ("Movie One", "Movie Two", "Movie Three"):
        assert movie in text
    assert "last checked 9:01 AM" in text


async def test_heartbeat_ids_dedupe_per_day_unless_asked_for(
    store: Store, fake_clock: FakeClock
) -> None:
    daily = await heartbeat_event(store, fake_clock)
    later = await heartbeat_event(store, fake_clock)
    fake_clock.advance(24 * 3600)
    tomorrow = await heartbeat_event(store, fake_clock)
    manual = await heartbeat_event(store, fake_clock, manual=True)
    assert daily.event_id == later.event_id != tomorrow.event_id
    assert manual.event_id != tomorrow.event_id


async def test_t5_6_factory_fails_clearly_for_telegram_and_works_for_console(
    store: Store, fake_clock: FakeClock, client: httpx.AsyncClient
) -> None:
    from showwatch.notify.factory import build_notifier

    with pytest.raises(NotifierConfigError, match="TELEGRAM_BOT_TOKEN"):
        build_notifier("telegram", {}, store=store, client=client, clock=fake_clock, tz=IST)
    stream = io.StringIO()
    console = build_notifier(
        "console", {}, store=store, client=client, clock=fake_clock, tz=IST, stream=stream
    )
    await console.send(sample(fake_clock))
    await console.send(sample(fake_clock))  # deduped
    assert stream.getvalue().count("Seats held") == 1


@respx.mock
async def test_giving_up_raises_an_error_without_the_url(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    # Checked without log redaction: the exception itself must be clean, wherever it ends up.
    respx.post(API).mock(side_effect=httpx.ConnectError(f"cannot reach {API}"))
    with pytest.raises(NotificationFailed) as info:
        await telegram(client, fake_clock).send(sample(fake_clock))
    assert TOKEN not in str(info.value)
    assert "ConnectError" in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None  # no chained httpx error carrying the URL


def test_any_token_is_registered_even_if_the_pattern_would_miss_it(
    redaction: None, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    # Backstop for a future token format the regex does not know.
    odd = "odd-format-token-from-the-future"
    TelegramNotifier(TelegramConfig(odd, CHAT), client=client, clock=fake_clock, tz=IST)
    assert odd not in swlog.redact(f"POST https://api.telegram.org/bot{odd}/sendMessage")
