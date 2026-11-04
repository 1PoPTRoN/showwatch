"""Locator healing (T9.3-T9.6): when the site renames a button, ask Claude what replaced it.

Only called when a booking step's element is missing, so never on the happy path (T9.7). The
executor allows at most one heal per step per attempt. Safeguards, all checked here:
  * never on the payment page: nothing from it is ever sent (C1);
  * only role-based locators are healed, and the answer must keep the expected role;
  * the proposed element must be exactly one *visible* element;
  * it must not be a forbidden locator ("Continue to payment", "Pay now");
  * its name must not mention UPI, OTP, card, CVV, PIN or password.
An accepted locator is stored as *provisional*, and a canary promotes or discards it (T9.4).

What leaves the machine: one screenshot and the accessibility snapshot of the current (non-
payment) page. That is why the healer is off unless `llm.healer: true` is set.
"""

import base64
import re

from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict

from showwatch.adapters.base import LocatorSpec, SiteAdapter
from showwatch.errors import HealFailed
from showwatch.executor.guards import Step
from showwatch.llm.client import Block, StructuredClaude

SENSITIVE = re.compile(r"\b(upi|otp|card|cvv|pin|password|passcode)\b", re.IGNORECASE)
MAX_SNAPSHOT_CHARS = 20_000

_SYSTEM = """You help a ticket-booking assistant whose booking site renamed or moved a button.
You get a screenshot of the current page and its accessibility snapshot. Name the one element
that now performs the given booking step, exactly as it appears in the snapshot: its ARIA role and
its accessible name. The step never involves payment; never choose anything that pays, or that
asks for UPI, card, CVV, OTP or password details."""


class LocatorProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    name: str
    why: str


class Healer:
    def __init__(self, claude: StructuredClaude, *, adapter: SiteAdapter) -> None:
        self._claude = claude
        self._adapter = adapter

    async def heal(self, page: Page, *, step: Step, name: str, spec: LocatorSpec) -> LocatorSpec:
        """A safe replacement for `spec`, or HealFailed (an LlmError) with the reason."""
        if spec.role is None:
            raise HealFailed(f"{name} is not found by role, so it cannot be healed")
        if await self._adapter.is_payment_page(page):
            raise HealFailed("refusing to look at the payment page")
        screenshot = base64.standard_b64encode(await page.screenshot(type="png")).decode("ascii")
        snapshot = (await page.locator("body").aria_snapshot())[:MAX_SNAPSHOT_CHARS]
        old = spec.name or spec.name_pattern or "(unnamed)"
        content: list[Block] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": screenshot},
            },
            {
                "type": "text",
                "text": (
                    f"Booking step: {step.value} (locator {name}).\n"
                    f"Expected role: {spec.role}. Its accessible name used to be: {old}.\n"
                    f"Accessibility snapshot of this page:\n{snapshot}"
                ),
            },
        ]
        proposal = await self._claude.ask(
            system=_SYSTEM, content=content, schema=LocatorProposal, effort="medium"
        )
        return await self.vet_proposal(page, proposal, spec)

    async def vet_proposal(
        self, page: Page, proposal: LocatorProposal, spec: LocatorSpec
    ) -> LocatorSpec:
        if SENSITIVE.search(proposal.name):
            raise HealFailed(
                f"{proposal.name!r} looks like a payment or identity field (sensitive)"
            )
        if proposal.role != spec.role:
            raise HealFailed(f"proposed role {proposal.role!r}, expected {spec.role!r} (role)")
        if await self._adapter.is_payment_page(page):
            raise HealFailed("refusing to act on the payment page")
        candidate = LocatorSpec(role=proposal.role, name=proposal.name, navigates=spec.navigates)
        locator = candidate.resolve(page)
        for forbidden_name, forbidden in self._adapter.forbidden().items():
            if await locator.and_(forbidden.resolve(page)).count() > 0:
                raise HealFailed(f"{proposal.name!r} is the {forbidden_name} button (forbidden)")
        visible = await locator.filter(visible=True).count()
        if visible != 1:
            raise HealFailed(
                f"{proposal.name!r} matches {visible} visible elements, not exactly one"
            )
        return candidate
