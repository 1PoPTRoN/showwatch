"""Phase 9: the locator healer, end to end with the Claude API mocked (tests T9.3-T9.7).

When a booking step's element is missing (the site renamed a button), the healer sends a
screenshot and an accessibility snapshot of that page, never of the payment page, and asks
which element now does the job. The proposal is used only if it names exactly one visible element
with the expected role, is not forbidden, and mentions no UPI/OTP/card/CVV. It is stored as
provisional until the next canary passes.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import httpx2
import pytest

from showwatch.adapters.base import LocatorSpec
from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.canary import Canary
from showwatch.errors import HealFailed
from showwatch.executor.executor import Executor, Outcome
from showwatch.executor.guards import Step
from showwatch.llm.client import StructuredClaude
from showwatch.llm.healer import Healer
from showwatch.models import EventKind, Snapshot
from showwatch.ratelimit import PollGate
from showwatch.state import State, Trigger
from showwatch.store import Store, TransitionResult
from showwatch.watcher.diff import diff, relevant_view
from tests.support.browser import BrowserSession
from tests.support.clock import FakeClock
from tests.support.fakesite import FakeSite
from tests.support.llm import FakeClaudeAPI, text_reply
from tests.support.notify import RecordingNotifier
from tests.support.targets import make_target

IST = ZoneInfo("Asia/Kolkata")
SHOW = {
    "show_id": "fri-a-19",
    "venue": "Cinema A",
    "date": "2026-12-18",
    "time": "19:00",
    "format": "IMAX 2D",
    "min_price": 450,
}
PROCEED = LocatorSpec(role="button", name_pattern=r"^Pay ₹\d+$", navigates=True)


def proposal(role: str, name: str) -> dict[str, Any]:
    return text_reply({"role": role, "name": name, "why": "it is the button that moves on (test)"})


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


@pytest.fixture
def adapter(
    fakesite: FakeSite, fake_clock: FakeClock, client: httpx.AsyncClient
) -> FakeSiteAdapter:
    fakesite.set(booking_open=True, shows=[SHOW])
    return FakeSiteAdapter(fakesite.base_url, client=client, clock=fake_clock)


def healer_for(api: FakeClaudeAPI, adapter: FakeSiteAdapter) -> Healer:
    return Healer(
        StructuredClaude(api.client(), model="claude-opus-5-5", max_retries=0), adapter=adapter
    )


async def detect(store: Store, adapter: FakeSiteAdapter) -> TransitionResult:
    target = make_target(id="t1", mode="assist")
    await store.add_target(target)
    await store.fire("t1", Trigger.START)
    snapshot = await adapter.check_availability(target)
    changes = diff(Snapshot("t1", snapshot.taken_at, {}), relevant_view(snapshot, target, tz=IST))
    return await store.fire("t1", Trigger.CHANGE_DETECTED, changes=changes)


def executor(
    store: Store,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
    clock: FakeClock,
    runs: Path,
    notifier: RecordingNotifier,
    healer: Healer | None,
    on_step: Any = None,
) -> Executor:
    return Executor(
        store=store,
        adapter=adapter,
        notifier=notifier,
        clock=clock,
        gate=PollGate(clock),
        tz=IST,
        open_page=browser.context.new_page,
        runs_dir=runs,
        step_timeout_ms=1_500,
        healer=healer,
        on_step=on_step,
    )


# --- T9.3 / T9.4 -----------------------------------------------------------------------------


async def test_t9_3_a_renamed_button_is_healed_and_cached_as_provisional(
    store: Store,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
    fake_clock: FakeClock,
    tmp_path: Path,
    fakesite: FakeSite,
) -> None:
    fakesite.set(rename_locator={"proceed_to_summary": "Book now"})
    api = FakeClaudeAPI(proposal("button", "Book now"))
    notifier = RecordingNotifier()
    run = executor(
        store, adapter, browser, fake_clock, tmp_path, notifier, healer_for(api, adapter)
    )

    result = await run.run(await detect(store, adapter))

    assert result.outcome is Outcome.HELD, result.error
    assert (await store.get_target("t1")).state is State.AWAITING_PAYMENT
    (body,) = api.requests  # exactly one heal call
    content = body["messages"][0]["content"]
    image = next(block for block in content if block["type"] == "image")
    assert image["source"]["media_type"] == "image/png"
    snapshot_text = " ".join(block["text"] for block in content if block["type"] == "text")
    assert "Book now" in snapshot_text  # the accessibility snapshot of the seat map page
    assert "proceed_to_summary" in snapshot_text
    overrides = await store.locator_overrides()
    assert overrides["proceed_to_summary"].status == "provisional"
    assert overrides["proceed_to_summary"].spec == LocatorSpec(
        role="button", name="Book now", navigates=True
    )


async def test_t9_4_a_provisional_locator_is_promoted_when_the_canary_passes(
    store: Store,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
    fake_clock: FakeClock,
    fakesite: FakeSite,
) -> None:
    fakesite.set(rename_locator={"proceed_to_summary": "Book now"})
    await store.save_provisional_locator(
        "proceed_to_summary", LocatorSpec(role="button", name="Book now", navigates=True)
    )
    canary = Canary(
        adapter=adapter,
        notifier=RecordingNotifier(),
        clock=fake_clock,
        open_page=browser.context.new_page,
        step_timeout_ms=1_500,
        store=store,
    )

    assert (await canary.run(fakesite.url("/show/fri-a-19"))).passed
    assert (await store.locator_overrides())["proceed_to_summary"].status == "confirmed"


async def test_t9_4_a_provisional_locator_is_discarded_when_the_canary_fails(
    store: Store,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
    fake_clock: FakeClock,
    fakesite: FakeSite,
) -> None:
    # The site changed its mind (no rename any more), so "Book now" no longer exists.
    await store.save_provisional_locator(
        "proceed_to_summary", LocatorSpec(role="button", name="Book now", navigates=True)
    )
    canary = Canary(
        adapter=adapter,
        notifier=RecordingNotifier(),
        clock=fake_clock,
        open_page=browser.context.new_page,
        step_timeout_ms=1_500,
        store=store,
    )

    result = await canary.run(fakesite.url("/show/fri-a-19"))
    assert not result.passed
    assert "proceed_to_summary" not in await store.locator_overrides()


# --- T9.5 ------------------------------------------------------------------------------------


async def test_t9_5_the_healer_never_looks_at_the_payment_page(
    adapter: FakeSiteAdapter, browser: BrowserSession, fakesite: FakeSite
) -> None:
    response = await asyncio.to_thread(
        httpx.post, fakesite.url("/show/fri-a-19/hold"), data={"qty": "2", "selected": "H5,H6"}
    )
    hold = re.search(r"/summary/(\w+)", response.headers["location"])

    assert hold
    page = await browser.context.new_page()
    await page.goto(fakesite.url(f"/payment/{hold[1]}"))
    api = FakeClaudeAPI()  # any request would fail the test
    with pytest.raises(HealFailed, match="payment page"):
        await healer_for(api, adapter).heal(
            page, step=Step.PROCEED_TO_SUMMARY, name="proceed_to_summary", spec=PROCEED
        )
    assert api.requests == []  # no screenshot of the payment page was ever sent


@pytest.mark.parametrize(
    ("page_html", "role", "name", "reason"),
    [
        pytest.param(
            "<button>Pay with UPI</button>", "button", "Pay with UPI", "sensitive", id="upi"
        ),
        pytest.param("<button>Enter OTP</button>", "button", "Enter OTP", "sensitive", id="otp"),
        pytest.param(
            "<button>Card details</button>", "button", "Card details", "sensitive", id="card"
        ),
        pytest.param("<button>CVV help</button>", "button", "CVV help", "sensitive", id="cvv"),
        pytest.param(
            '<a href="#">Continue to payment</a>',
            "link",
            "Continue to payment",
            "forbidden",
            id="forbidden",
        ),
        pytest.param(
            "<button>Go</button><button>Go</button>", "button", "Go", "exactly one", id="ambiguous"
        ),
        pytest.param("<button>Go</button>", "button", "Gone", "exactly one", id="missing"),
        pytest.param('<a href="#">Book now</a>', "link", "Book now", "role", id="wrong-role"),
        pytest.param(
            '<button style="display:none">Book now</button>',
            "button",
            "Book now",
            "exactly one",
            id="invisible",
        ),
        # In the accessibility tree, but no size on screen (a collapsed menu): unclickable.
        pytest.param(
            '<button style="width:0;height:0;padding:0;border:0;overflow:hidden">Book now</button>',
            "button",
            "Book now",
            "exactly one",
            id="zero-size",
        ),
    ],
)
async def test_t9_5_unsafe_or_unclear_proposals_are_rejected(
    page_html: str,
    role: str,
    name: str,
    reason: str,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
) -> None:
    page = await browser.context.new_page()
    await page.set_content(
        f"<main data-page='seats'>{page_html}</main>"
    )  # local content, no request
    expected = (
        PROCEED if reason != "forbidden" else LocatorSpec(role="link", name="Next", navigates=True)
    )
    api = FakeClaudeAPI(proposal(role, name))
    with pytest.raises(HealFailed, match=reason):
        await healer_for(api, adapter).heal(
            page, step=Step.PROCEED_TO_SUMMARY, name="proceed_to_summary", spec=expected
        )


# --- T9.6 / T9.7 -----------------------------------------------------------------------------


async def test_t9_6_an_llm_timeout_fails_the_attempt_after_one_heal_call(
    store: Store,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
    fake_clock: FakeClock,
    tmp_path: Path,
    fakesite: FakeSite,
) -> None:
    fakesite.set(rename_locator={"proceed_to_summary": "Book now"})
    api = FakeClaudeAPI(httpx2.ReadTimeout("slow"), proposal("button", "Book now"))
    notifier = RecordingNotifier()
    run = executor(
        store, adapter, browser, fake_clock, tmp_path, notifier, healer_for(api, adapter)
    )

    result = await run.run(await detect(store, adapter))

    assert result.outcome is Outcome.FAILED
    assert (await store.get_target("t1")).state is State.FAILED
    assert len(api.requests) == 1  # at most one heal call for this step in this attempt
    assert [e.kind for e in notifier.events] == [EventKind.FAILED]
    assert await store.locator_overrides() == {}


async def test_t9_7_the_happy_path_never_calls_the_healer(
    store: Store,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    api = FakeClaudeAPI()  # any request would fail the test
    run = executor(
        store, adapter, browser, fake_clock, tmp_path, RecordingNotifier(), healer_for(api, adapter)
    )
    result = await run.run(await detect(store, adapter))
    assert result.outcome is Outcome.HELD
    assert api.requests == []


async def test_t9_6_a_step_is_healed_at_most_once_per_attempt(
    store: Store,
    adapter: FakeSiteAdapter,
    browser: BrowserSession,
    fake_clock: FakeClock,
    tmp_path: Path,
    fakesite: FakeSite,
) -> None:
    # The first heal works; then a lost seat race restarts the flow, and by then the site has
    # renamed the button again. A second heal for the same step in the same attempt is not allowed.
    fakesite.set(rename_locator={"select_seats": "Choose seats"}, steal_seats_on_click=1)
    opened = 0

    async def rename_again_on_retry(step: Step) -> None:
        nonlocal opened
        if step is Step.OPEN_SHOW:
            opened += 1
            if opened == 2:
                fakesite.set(rename_locator={"select_seats": "Pick seats"})

    api = FakeClaudeAPI(proposal("button", "Choose seats"), proposal("button", "Pick seats"))
    run = executor(
        store,
        adapter,
        browser,
        fake_clock,
        tmp_path,
        RecordingNotifier(),
        healer_for(api, adapter),
        on_step=rename_again_on_retry,
    )

    result = await run.run(await detect(store, adapter))

    assert len(api.requests) == 1
    assert result.outcome is Outcome.FAILED
    assert [r.reason for r in await store.audit("t1")][-1] == "locator_not_found:choose_quantity"
