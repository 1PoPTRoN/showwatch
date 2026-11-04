"""A fake Claude API for tests: the real `anthropic` SDK, served canned responses.

`httpx2.MockTransport` sits under the SDK, so request building and response parsing are the
real ones. No socket is opened and no key is needed. Every request body is recorded.
"""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2

Reply = dict[str, Any] | Exception | Callable[[dict[str, Any]], dict[str, Any]]


def text_reply(payload: object, *, model: str = "claude-opus-5-5") -> dict[str, Any]:
    """A Messages API response whose text is `payload` as JSON (what structured output returns)."""
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def refusal_reply() -> dict[str, Any]:
    reply = text_reply({})
    reply.update(
        content=[],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": None, "explanation": "declined (test)"},
    )
    return reply


class FakeClaudeAPI:
    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if not self._replies:
            raise AssertionError("the fake Claude API got an unexpected request")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        payload = reply(body) if callable(reply) else reply
        return httpx2.Response(200, json=payload)

    def client(self) -> anthropic.AsyncAnthropic:
        transport = httpx2.MockTransport(self._handle)
        return anthropic.AsyncAnthropic(
            api_key="sk-ant-test-not-a-real-key",
            http_client=anthropic.DefaultAsyncHttpxClient(transport=transport),
            max_retries=0,
        )
