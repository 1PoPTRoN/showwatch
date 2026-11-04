"""Phase 9: plain language → target (tests T9.1, T9.2), with the Claude API mocked.

The model only drafts. Its answer goes through the same strict validation as a YAML file
(past dates, secrets, limits), and the result is always notify-only. Nothing is saved here:
saving needs the user's confirmation (tested through the CLI in tests/e2e/test_cli.py).
"""

from datetime import date
from typing import Any

import pytest

from showwatch.config import Mode, Settings
from showwatch.errors import ConfigError, LlmError
from showwatch.llm.client import DEFAULT_MODEL, FALLBACK_BETA, StructuredClaude, resolve_model
from showwatch.llm.intent_parser import IntentParser, target_yaml
from tests.support.clock import FakeClock
from tests.support.llm import FakeClaudeAPI, refusal_reply, text_reply

REQUEST = "2 IMAX tickets for Monsoon Nights this Friday evening in Bengaluru, rows H to K"
DRAFT: dict[str, Any] = {
    "target_id": "monsoon-nights-fri",
    "movie": "Monsoon Nights",
    "city": "bengaluru",
    "language": "hindi",
    "formats": ["IMAX 2D", "IMAX 3D"],
    "venues": [],
    "dates": ["2026-11-06"],  # "this Friday" from Tue 3 Nov
    "showtime_start": "18:00",
    "showtime_end": "22:30",
    "seats": 2,
    "preferred_rows": ["H", "J", "K"],
    "avoid_rows": [],
    "max_price_per_seat": 1000,
}


def parser(api: FakeClaudeAPI, clock: FakeClock) -> IntentParser:
    # No SDK retries here: each scripted reply answers exactly one request, and retries would
    # back off in real time.
    return IntentParser(
        StructuredClaude(api.client(), model=DEFAULT_MODEL, max_retries=0), clock=clock
    )


async def test_t9_1_a_plain_request_becomes_a_valid_target(fake_clock: FakeClock) -> None:
    api = FakeClaudeAPI(text_reply(DRAFT))
    target = await parser(api, fake_clock).parse(REQUEST)

    assert target.id == "monsoon-nights-fri"
    assert target.mode is Mode.NOTIFY_ONLY  # never assist: the user raises it on purpose
    assert target.dates == (date(2026, 11, 6),)
    assert target.preferred_rows == ("H", "J", "K")
    assert (target.showtime_window.start.hour, target.showtime_window.end.hour) == (18, 22)

    shown = target_yaml(target)
    assert "monsoon-nights-fri" in shown
    assert 'start: "18:00"' in shown or "start: '18:00'" in shown
    reparsed = Settings.model_validate({"version": 1, "targets": [target.model_dump(mode="json")]})
    assert reparsed.targets[0] == target


async def test_t9_1_the_request_carries_the_text_today_and_the_safeguards(
    fake_clock: FakeClock,
) -> None:
    api = FakeClaudeAPI(text_reply(DRAFT))
    await parser(api, fake_clock).parse(REQUEST)

    (body,) = api.requests
    assert body["model"] == DEFAULT_MODEL
    assert body["fallbacks"] == "default"  # server-side refusal fallback, opted in by default
    assert FALLBACK_BETA in api.headers[0]["anthropic-beta"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in body  # forcing a tool is a 400 on this model
    assert REQUEST in str(body["messages"])
    assert "Tuesday 3 November 2026" in str(body["system"])  # so "this Friday" resolves


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param({"seats": 11}, "seats must be between 1 and 10", id="rule-violation"),
        pytest.param({"dates": ["2026-10-30"]}, "is in the past", id="past-date"),
        pytest.param({"showtime_start": "18:00pm"}, "HH:MM", id="bad-time"),
    ],
)
async def test_t9_2_answers_that_break_the_rules_are_rejected_with_reasons(
    change: dict[str, Any], expected: str, fake_clock: FakeClock
) -> None:
    api = FakeClaudeAPI(text_reply({**DRAFT, **change}))
    with pytest.raises(ConfigError, match=expected):
        await parser(api, fake_clock).parse(REQUEST)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({**DRAFT, "telegram_token": "123"}, id="extra-field"),
        pytest.param({k: v for k, v in DRAFT.items() if k != "seats"}, id="missing-field"),
    ],
)
async def test_t9_2_answers_with_the_wrong_shape_are_rejected(
    answer: dict[str, Any], fake_clock: FakeClock
) -> None:
    api = FakeClaudeAPI(text_reply(answer))
    with pytest.raises(LlmError, match="did not match"):
        await parser(api, fake_clock).parse(REQUEST)


async def test_a_declined_request_is_an_llm_error(fake_clock: FakeClock) -> None:
    with pytest.raises(LlmError, match="declined"):
        await parser(FakeClaudeAPI(refusal_reply()), fake_clock).parse(REQUEST)


async def test_a_timeout_is_an_llm_error(fake_clock: FakeClock) -> None:
    import httpx2

    with pytest.raises(LlmError, match="timed out"):
        await parser(FakeClaudeAPI(httpx2.ReadTimeout("slow")), fake_clock).parse(REQUEST)


def test_model_comes_from_env_then_config_then_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = Settings.model_validate(
        {"version": 1, "llm": {"model": "claude-sonnet-5-5"}, "targets": [_target()]}
    )
    monkeypatch.delenv("SHOWWATCH_LLM_MODEL", raising=False)
    assert resolve_model(None) == DEFAULT_MODEL == "claude-opus-5-5"
    assert resolve_model(configured) == "claude-sonnet-5-5"
    monkeypatch.setenv("SHOWWATCH_LLM_MODEL", "claude-haiku-5-5")
    assert resolve_model(configured) == "claude-haiku-5-5"


def test_the_healer_is_off_unless_switched_on() -> None:
    assert Settings.model_validate({"version": 1, "targets": [_target()]}).llm.healer is False


def _target() -> dict[str, Any]:
    from tests.support.targets import make_target

    return make_target().model_dump(mode="json")
