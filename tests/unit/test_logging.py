"""Phase 5: log redaction and JSON-lines logging (C7).

Redaction runs when a log record is *created* (a LogRecord factory), so it covers every logger
(httpx included), every handler, and pytest's own log capture.
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from showwatch import logging as swlog

TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


@pytest.fixture
def redaction() -> Iterator[None]:
    uninstall = swlog.install_redaction()
    try:
        yield
    finally:
        uninstall()
        swlog.forget_secrets()


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"POST https://api.telegram.org/bot{TOKEN}/sendMessage 200", TOKEN),
        (f"token is {TOKEN} ok", TOKEN),
        ("Cookie: session=abc123def456; other=1", "session=abc123def456"),
        ('{"set-cookie": "sid=Zm9vYmFyYmF6"}', "sid=Zm9vYmFyYmF6"),
        ("GET https://site.example/x?user=1&token=s3cr3tvalue&page=2", "s3cr3tvalue"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("key sk-ant-api03-AbCdEf123456_-xyz used", "sk-ant-api03-AbCdEf123456_-xyz"),
    ],
)
def test_known_secret_shapes_are_redacted(text: str, secret: str) -> None:
    redacted = swlog.redact(text)
    assert secret not in redacted
    assert "<redacted>" in redacted


def test_registered_secrets_are_redacted(redaction: None) -> None:
    swlog.register_secret("/Users/me/.showwatch/profile")
    assert "/Users/me/.showwatch/profile" not in swlog.redact(
        "using /Users/me/.showwatch/profile now"
    )


def test_ordinary_text_is_left_alone() -> None:
    text = "Fri 18 Dec, 19:00 IMAX 2D at Cinema A, seats H9-H10, price 450, id movie-fri-ab12cd"
    assert swlog.redact(text) == text


def test_short_values_are_never_registered_as_secrets(redaction: None) -> None:
    swlog.register_secret("A")  # would otherwise blank every capital A
    assert swlog.redact("A seat") == "A seat"


def test_records_are_redacted_when_created(
    redaction: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx").info(
            'HTTP Request: POST %s "%s"', f"https://api.telegram.org/bot{TOKEN}/x", 200
        )
        try:
            raise RuntimeError(f"boom at https://api.telegram.org/bot{TOKEN}/sendMessage")
        except RuntimeError:
            logging.getLogger("showwatch.test").exception("failed")
    assert len(caplog.records) == 2
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in str(record.args)
        assert TOKEN not in (record.exc_text or "")
        assert record.exc_info is None  # the raw exception (and its URL) is not kept
    assert TOKEN not in caplog.text
    assert "RuntimeError" in (caplog.records[1].exc_text or "")  # the traceback is still useful


def test_uninstall_restores_the_previous_factory() -> None:
    before = logging.getLogRecordFactory()
    uninstall = swlog.install_redaction()
    assert logging.getLogRecordFactory() is not before
    uninstall()
    assert logging.getLogRecordFactory() is before


def test_json_lines_log_file(tmp_path: Path) -> None:
    undo = swlog.configure_logging(tmp_path / "logs", level=logging.INFO)
    try:
        logging.getLogger("showwatch.test").info("polled %s via bot%s", "movie-fri", TOKEN)
        logging.getLogger("httpx").info(
            "HTTP Request: POST https://api.telegram.org/bot%s/x", TOKEN
        )
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    finally:
        undo()
        swlog.forget_secrets()

    files = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert [line["logger"] for line in lines] == ["showwatch.test"]  # httpx INFO is silenced
    entry = lines[0]
    assert entry["level"] == "INFO"
    assert entry["at"].endswith("+00:00")
    assert "movie-fri" in entry["message"]
    assert TOKEN not in files[0].read_text()
