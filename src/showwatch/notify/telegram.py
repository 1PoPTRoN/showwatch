"""Telegram notifier (SPEC §7 Notifier), over an injected httpx client.

* The bot token lives only in the request URL. It is registered with the log redactor, kept out
  of repr(), and never put into an exception or log message: errors name the status, not the URL.
* Retries, waiting on the injected clock: 5xx and network errors get up to 3 retries with
  1, 2 and 4 s backoff; a 429 waits Telegram's `retry_after` (capped). Any other 4xx (a bad token,
  an unknown chat) fails at once.
* After the last try it raises NotificationFailed; DedupNotifier logs it and the pipeline goes on.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self
from zoneinfo import ZoneInfo

import httpx

from showwatch.clock import Clock
from showwatch.errors import NotificationFailed, NotifierConfigError
from showwatch.logging import redact, register_secret
from showwatch.models import Event
from showwatch.notify.messages import render

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
RETRIES = 3
BACKOFF_S = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 60.0

_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
_CHAT = re.compile(r"-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,}")


@dataclass(frozen=True)
class TelegramConfig:
    token: str = field(repr=False)
    chat_id: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self:
        """Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Errors never echo the values."""
        token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = environ.get("TELEGRAM_CHAT_ID", "").strip()
        missing = [
            name
            for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
            if not value
        ]
        if missing:
            raise NotifierConfigError(
                f"Telegram notifications need {' and '.join(missing)} in the environment. "
                "Put them in .env (see .env.example), or set `notifier: console` in targets.yaml."
            )
        problems = []
        if not _TOKEN.fullmatch(token):
            problems.append(
                "TELEGRAM_BOT_TOKEN does not look like a bot token "
                "(expected <digits>:<35 characters>, as @BotFather gives it)"
            )
        if not _CHAT.fullmatch(chat_id):
            problems.append("TELEGRAM_CHAT_ID must be a numeric chat id or an @channel name")
        if problems:
            raise NotifierConfigError("; ".join(problems) + ". Fix it in .env.")
        register_secret(token)
        return cls(token, chat_id)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class TelegramNotifier:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        client: httpx.AsyncClient,
        clock: Clock,
        tz: ZoneInfo,
        timeout_s: float = 10.0,
    ) -> None:
        register_secret(config.token)
        self._url = f"{API_BASE}/bot{config.token}/sendMessage"
        self._chat_id = config.chat_id
        self._client = client
        self._clock = clock
        self._tz = tz
        self._timeout_s = timeout_s

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self._chat_id!r})"

    async def send(self, event: Event) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": render(event, tz=self._tz, html=True),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        problem = "no attempt made"
        for attempt in range(RETRIES + 1):
            try:
                response = await self._client.post(self._url, json=payload, timeout=self._timeout_s)
            except httpx.TransportError as exc:  # its message holds the URL: never show it
                problem = f"network error ({type(exc).__name__})"
                wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            else:
                body = _json(response)
                if response.status_code == 200 and body.get("ok", True) is True:
                    return
                if response.status_code == 429:
                    problem = "rate limited by Telegram (429)"
                    asked = body.get("parameters", {}).get("retry_after")
                    wait = (
                        min(float(asked), MAX_RETRY_AFTER_S)
                        if isinstance(asked, int | float)
                        else BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                    )
                elif response.status_code >= 500:
                    problem = f"Telegram server error ({response.status_code})"
                    wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                else:
                    description = str(body.get("description", ""))[:200]
                    raise NotificationFailed(
                        redact(
                            f"Telegram refused the message ({response.status_code}: {description})"
                        )
                    )
            if attempt == RETRIES:
                break
            log.warning("Telegram: %s; retrying in %.0f s", problem, wait)
            await self._clock.sleep(wait)
        raise NotificationFailed(f"Telegram: gave up after {RETRIES + 1} tries: {problem}")
