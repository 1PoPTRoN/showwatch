"""Logging with redaction (C7): JSON lines in ~/.showwatch/logs/, and no secrets anywhere.

Redaction happens when a log record is *created*, through a LogRecord factory. A
`logging.Filter` on a logger never sees records from child loggers such as `httpx`, and pytest's
`caplog` has its own handler, so redacting at creation is the only place that covers every
logger and every handler. At creation the message is formatted and redacted, and any exception
is turned into redacted text. The raw exception object, whose message may hold a token-bearing
URL, is not kept. A handler filter repeats the redaction as a second layer.

Telegram Bot API URLs carry the bot token, and httpx logs request URLs at INFO. Besides the
redaction, `configure_logging` turns httpx and httpcore down to WARNING.
"""

import json
import logging
import re
import threading
import traceback
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

REDACTED = "<redacted>"
_MIN_SECRET_LENGTH = 8

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Telegram bot tokens, in Bot API URLs or bare: <bot id>:<35 characters>
    (re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}"), REDACTED),
    # Anthropic API keys
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), REDACTED),
    # Cookie, Set-Cookie and Authorization header values (also in JSON-ish dumps)
    (
        re.compile(r"""(?i)\b(set-cookie|cookie|authorization)(["']?\s*[:=]\s*["']?)[^\r\n"']+"""),
        rf"\1\2{REDACTED}",
    ),
    # Secret-looking query parameters
    (
        re.compile(
            r"(?i)([?&](?:token|key|api_key|apikey|sig|signature|auth|session|sid)=)[^&\s#\"']+"
        ),
        rf"\1{REDACTED}",
    ),
)

_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Redact this exact value wherever it appears (bot token, browser profile path, ...)."""
    value = value.strip()
    if len(value) >= _MIN_SECRET_LENGTH:
        with _secrets_lock:
            _secrets.add(value)


def forget_secrets() -> None:
    """Forget every registered secret (for tests)."""
    with _secrets_lock:
        _secrets.clear()


def redact(text: str) -> str:
    with _secrets_lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 (a malformed log call must still be logged, redacted)
        message = f"{record.msg!s} {record.args!s}"
    record.msg = redact(message)
    record.args = None
    if record.exc_info:
        record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip())
        record.exc_info = None
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
    return record


def install_redaction() -> Callable[[], None]:
    """Redact every log record from now on. Returns a function that undoes it."""
    previous = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        return _redact_record(previous(*args, **kwargs))

    logging.setLogRecordFactory(factory)

    def uninstall() -> None:
        logging.setLogRecordFactory(previous)

    return uninstall


class RedactingFilter(logging.Filter):
    """Second layer: redact again at the handler, in case a record skipped the factory."""

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, str] = {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            entry["exc"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(
    log_dir: Path, *, level: int = logging.INFO, secrets: Iterable[str] = ()
) -> Callable[[], None]:
    """Log JSON lines to `log_dir/showwatch.jsonl` (rotated), redacted. Returns an undo function."""
    for secret in secrets:
        register_secret(secret)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "showwatch.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(JsonLinesFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    previous_level = root.level
    quiet = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    uninstall = install_redaction()
    root.addHandler(handler)
    root.setLevel(level)
    for name in quiet:
        logging.getLogger(name).setLevel(logging.WARNING)

    def undo() -> None:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous_level)
        for name, old in quiet.items():
            logging.getLogger(name).setLevel(old)
        uninstall()

    return undo
