"""The payment boundary (C1). The only module allowed to click on, or choose options in, a page.

* Booking steps are an allow-list (`Step`), and it has no payment step.
* Before every action: refuse if the page is the payment page, or if the target element is one of
  the adapter's forbidden locators (for example "Continue to payment"). The check comes first, so
  a refused click sends nothing.
* Nothing in ShowWatch types into a page. tests/unit/test_architecture.py enforces both rules.
"""

from enum import StrEnum

from playwright.async_api import Locator, Page

from showwatch.adapters.base import SiteAdapter
from showwatch.errors import PaymentBoundaryError


class Step(StrEnum):
    OPEN_SHOW = "open_show"
    CHOOSE_QUANTITY = "choose_quantity"
    SELECT_SEAT = "select_seat"
    PROCEED_TO_SUMMARY = "proceed_to_summary"


async def _check_boundary(page: Page, target: Locator, adapter: SiteAdapter, step: Step) -> None:
    if await adapter.is_payment_page(page):
        raise PaymentBoundaryError(f"refusing to act on the payment page (step {step.value})")
    for name, spec in adapter.forbidden().items():
        if await target.and_(spec.resolve(page)).count() > 0:
            raise PaymentBoundaryError(f"refusing to click {name} (step {step.value})")


async def safe_click(
    page: Page,
    target: Locator,
    adapter: SiteAdapter,
    *,
    step: Step,
    navigates: bool = False,
) -> None:
    """Click `target` unless that would cross the payment boundary (PaymentBoundaryError)."""
    await _check_boundary(page, target, adapter, step)
    if navigates:
        async with page.expect_navigation():
            await target.click()
    else:
        await target.click()


async def safe_select(
    page: Page, target: Locator, value: str, adapter: SiteAdapter, *, step: Step
) -> None:
    """Choose `value` in a select element, under the same boundary checks as a click."""
    await _check_boundary(page, target, adapter, step)
    await target.select_option(value)
