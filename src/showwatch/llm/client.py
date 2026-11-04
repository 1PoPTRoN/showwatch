"""The one place ShowWatch talks to the Claude API: a structured question, a validated answer.

* Model: `SHOWWATCH_LLM_MODEL`, else `llm.model` in targets.yaml, else claude-opus-5-5.
* The answer is constrained to a pydantic schema (structured outputs). On this model a forced
  tool call is a 400, so the schema is the guarantee; tool_choice is never set.
* Server-side refusal fallback is on (`fallbacks: "default"`). A request that is still declined
  becomes an LlmError, never an exception the caller must know about.
* Timeouts, connection problems, API errors and answers that do not match the schema all become
  LlmError. The caller decides what that means (the booking step fails; nothing is guessed).
"""

import os
from collections.abc import Sequence
from typing import Literal

import anthropic
from anthropic.types.beta import BetaImageBlockParam, BetaTextBlockParam, BetaUserMessageParam
from pydantic import BaseModel, ValidationError

from showwatch.config import Settings
from showwatch.errors import LlmError

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16_000

Effort = Literal["low", "medium", "high"]
Block = BetaTextBlockParam | BetaImageBlockParam


def resolve_model(settings: Settings | None) -> str:
    return (
        os.environ.get("SHOWWATCH_LLM_MODEL")
        or (settings.llm.model if settings else None)
        or DEFAULT_MODEL
    )


class StructuredClaude:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self._client = client.with_options(timeout=timeout_s, max_retries=max_retries)
        self.model = model

    async def ask[T: BaseModel](
        self,
        *,
        system: str,
        content: str | Sequence[Block],
        schema: type[T],
        effort: Effort,
    ) -> T:
        message: BetaUserMessageParam = {"role": "user", "content": content}
        try:
            response = await self._client.beta.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[message],
                output_format=schema,
                output_config={"effort": effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'answer'}: {err['msg']}"
                for err in exc.errors()
            )
            raise LlmError(
                f"the model's answer did not match the expected shape ({problems})"
            ) from None
        except anthropic.APITimeoutError:
            raise LlmError("the request to Claude timed out") from None
        except anthropic.APIConnectionError:
            raise LlmError("could not reach the Claude API") from None
        except anthropic.APIStatusError as exc:
            raise LlmError(f"the Claude API answered {exc.status_code}") from None
        if response.stop_reason == "refusal":
            raise LlmError("Claude declined this request")
        if response.parsed_output is None:
            raise LlmError(f"no usable answer (stopped: {response.stop_reason})")
        return response.parsed_output
