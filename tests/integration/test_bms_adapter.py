"""Phase 10: BmsAdapter's availability check, HTTP tier (the browser tier is in tests/e2e).

BmsAdapter knows nothing about a site except its calibration file. Here it reads FakeShow through
tests/support/fakeshow-calibration.yaml, and must report exactly what FakeSiteAdapter reports.

Traffic (C4): a check reads one listing page per date. The first check of a target fetches every
date, each extra request paced through the gate; after that, each check refreshes only the date
read longest ago, so steady state is one request per check (docs/DESIGN-NOTES.md P10.3).
"""

from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from playwright.async_api import Page

from showwatch import __version__
from showwatch.adapters.bms import BmsAdapter
from showwatch.adapters.calibration import Calibration, parse_calibration
from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.config import Target
from showwatch.errors import Blocked, RateLimited, TransientError
from showwatch.models import Snapshot
from showwatch.ratelimit import PollGate
from tests.support.calibration import fakeshow_calibration, fakeshow_raw, listing_url
from tests.support.clock import FakeClock
from tests.support.fakesite import FakeSite, run_fakesite
from tests.support.targets import make_target

SHOWS: list[dict[str, Any]] = [
    {"show_id": "fri-a-19", "venue": "Cinema A", "date": "2026-12-18", "time": "19:00",
     "format": "IMAX 2D", "min_price": 450},
    {"show_id": "fri-b-21", "venue": "Cinema B", "date": "2026-12-18", "time": "21:00",
     "format": "4DX", "min_price": 600},
    {"show_id": "sat-a-18", "venue": "Cinema A", "date": "2026-12-19", "time": "18:30",
     "format": "2D", "min_price": 250},
]  # fmt: skip
FRI, SAT = date(2026, 12, 18), date(2026, 12, 19)
REMOTE = "https://cinema.example.test"


async def no_browser() -> Page:
    raise AssertionError("the HTTP tier must not open a browser")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


class Rig:
    def __init__(self, calibration: Calibration, client: httpx.AsyncClient, clock: FakeClock):
        self.gate = PollGate(clock)
        self.clock = clock
        self.adapter = BmsAdapter(
            calibration, client=client, clock=clock, gate=self.gate, open_page=no_browser
        )

    async def check(self, target: Target) -> Snapshot:
        async with self.gate.poll():  # as the watcher does
            return await self.adapter.check_availability(target)


@pytest.fixture
def rig(fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock) -> Rig:
    return Rig(fakeshow_calibration(fakesite.base_url), client, fake_clock)


def target_for(site: FakeSite, **overrides: Any) -> Target:
    return make_target(listing_url=listing_url(site.base_url), **overrides)


def queried(site: FakeSite) -> list[str]:
    return [e["query"] for e in site.log() if e["path"] == "/movie/shows"]


async def test_t10_2_reads_the_same_shows_as_the_fake_adapter(
    rig: Rig, fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    target = target_for(fakesite, dates=["2026-12-18", "2026-12-19"])
    expected = await FakeSiteAdapter(
        fakesite.base_url, client=client, clock=fake_clock
    ).check_availability(target)
    fakesite.reset_log()

    snapshot = await rig.check(target)

    assert dict(snapshot.open_dates) == dict(expected.open_dates)
    assert snapshot.target_id == target.id
    assert queried(fakesite) == ["date=2026-12-18", "date=2026-12-19"]  # one page per date
    assert fake_clock.sleeps == [20]  # the second page waited its turn at the gate


async def test_after_the_first_round_each_check_is_one_request(
    rig: Rig, fakesite: FakeSite
) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    target = target_for(fakesite, dates=["2026-12-18", "2026-12-19"])
    await rig.check(target)
    fakesite.reset_log()

    for expected in ("date=2026-12-18", "date=2026-12-19", "date=2026-12-18"):
        rig.clock.advance(60)
        snapshot = await rig.check(target)
        assert queried(fakesite) == [expected]  # the date read longest ago
        assert sorted(snapshot.open_dates) == [FRI, SAT]  # still a complete snapshot (§7)
        fakesite.reset_log()


async def test_a_newly_opened_date_is_seen_on_its_turn(rig: Rig, fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS[:2])  # Saturday not open yet
    target = target_for(fakesite, dates=["2026-12-18", "2026-12-19"])
    assert sorted((await rig.check(target)).open_dates) == [FRI]
    fakesite.set(shows=SHOWS)
    assert sorted((await rig.check(target)).open_dates) == [FRI]  # Friday's turn
    assert sorted((await rig.check(target)).open_dates) == [FRI, SAT]  # Saturday's turn


async def test_closed_booking_and_closed_dates_mean_nothing_open(
    rig: Rig, fakesite: FakeSite
) -> None:
    fakesite.set(booking_open=False, shows=SHOWS)
    assert dict((await rig.check(target_for(fakesite))).open_dates) == {}


async def test_a_date_redirected_to_another_date_is_not_open(rig: Rig, fakesite: FakeSite) -> None:
    # Some sites answer a date with no shows by showing the nearest open date instead. Those
    # shows must never be filed under the date that was asked for.
    fakesite.set(booking_open=True, shows=SHOWS[2:], closed_date_redirect=True)
    snapshot = await rig.check(target_for(fakesite))
    assert dict(snapshot.open_dates) == {}
    assert [e["query"] for e in fakesite.log()] == ["date=2026-12-18", "date=2026-12-19"]


@pytest.mark.parametrize(
    ("state", "error", "reason", "says"),
    [
        pytest.param({"status_code": 429}, RateLimited, None, "429", id="429"),
        pytest.param({"status_code": 403}, Blocked, "forbidden", "forbidden", id="403"),
        pytest.param({"interstitial": "captcha"}, Blocked, "captcha", "captcha", id="captcha"),
        pytest.param({"interstitial": "queue"}, Blocked, "queue", "queue", id="queue"),
        pytest.param({"status_code": 503}, TransientError, None, "503", id="503"),
        pytest.param({"interstitial": "login"}, TransientError, None, "log in", id="login"),
    ],
)
async def test_t6_4_site_trouble_maps_to_the_same_typed_errors(
    state: dict[str, object],
    error: type[Exception],
    reason: str | None,
    says: str,
    rig: Rig,
    fakesite: FakeSite,
) -> None:
    fakesite.set(booking_open=True, shows=SHOWS, **state)
    with pytest.raises(error, match=says) as info:
        await rig.check(target_for(fakesite))
    if reason is not None:
        assert isinstance(info.value, Blocked)
        assert info.value.reason == reason
    assert len(fakesite.log()) <= 2  # the request, plus FakeShow's redirect to its interstitial


async def test_a_blocked_page_is_never_retried_in_the_browser(
    fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    # C3: falling back to the browser is for pages that need JavaScript, never for getting past
    # a CAPTCHA, a queue or a block. The browser would be the same user getting around it.
    rig = Rig(fakeshow_calibration(fakesite.base_url), client, fake_clock)
    fakesite.set(booking_open=True, shows=SHOWS, listing_render="script", interstitial="captcha")
    with pytest.raises(Blocked):
        await rig.check(target_for(fakesite))  # no_browser would fail the test if it were used


async def test_a_page_that_needs_javascript_is_an_error_when_the_tier_is_http(
    fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    raw = fakeshow_raw(fakesite.base_url)
    raw["listing"]["tier"] = "http"
    rig = Rig(parse_calibration(raw), client, fake_clock)
    fakesite.set(booking_open=True, shows=SHOWS, listing_render="script")
    with pytest.raises(TransientError, match="JavaScript"):
        await rig.check(target_for(fakesite))


async def test_past_dates_are_not_fetched(rig: Rig, fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    target = target_for(fakesite, dates=["2026-11-02", "2026-12-18"])  # the clock is 3 Nov
    await rig.check(target)
    assert queried(fakesite) == ["date=2026-12-18"]


async def test_only_dates_still_wanted_are_reported(rig: Rig, fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    both = target_for(fakesite, dates=["2026-12-18", "2026-12-19"])
    await rig.check(both)
    friday_only = target_for(fakesite, dates=["2026-12-18"])
    assert sorted((await rig.check(friday_only)).open_dates) == [FRI]


async def test_a_target_needs_a_listing_url_on_the_calibrated_site(
    rig: Rig, fakesite: FakeSite
) -> None:
    with pytest.raises(TransientError, match="listing_url"):
        await rig.check(make_target())
    elsewhere = make_target(listing_url="http://203.0.113.10/movie/{date}")
    with pytest.raises(TransientError, match="not on the calibrated site"):
        await rig.check(elsewhere)
    assert fakesite.log() == []


async def test_unreachable_site_is_transient(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    with run_fakesite() as site:
        base = site.base_url
    rig = Rig(fakeshow_calibration(base), client, fake_clock)
    with pytest.raises(TransientError, match="ConnectError"):
        await rig.check(make_target(listing_url=listing_url(base)))


# --- with mocked HTTP (respx): what FakeShow does not do -----------------------------------------


def remote_rig(client: httpx.AsyncClient, clock: FakeClock) -> Rig:
    return Rig(fakeshow_calibration(REMOTE), client, clock)


@respx.mock
async def test_a_missing_listing_page_means_the_date_is_not_open_yet(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    # Before booking opens, a date's page often does not exist. That is "not open", not a failure
    # (which would back off for up to an hour: exactly when the date might open).
    respx.get(f"{REMOTE}/movie/shows").mock(return_value=httpx.Response(404, text="gone"))
    snapshot = await remote_rig(client, fake_clock).check(
        make_target(listing_url=listing_url(REMOTE))
    )
    assert dict(snapshot.open_dates) == {}


@respx.mock
async def test_requests_identify_showwatch_honestly(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    # C3: nothing is spoofed. No browser user agent, no copied browser headers, no rotation.
    route = respx.get(f"{REMOTE}/movie/shows").mock(return_value=httpx.Response(404))
    await remote_rig(client, fake_clock).check(make_target(listing_url=listing_url(REMOTE)))
    (call,) = route.calls
    headers = {k.lower(): v for k, v in call.request.headers.items()}
    assert headers["user-agent"] == f"showwatch/{__version__}"
    assert set(headers) <= {"host", "accept", "accept-encoding", "connection", "user-agent"}
    assert "cookie" not in headers


@respx.mock
async def test_a_server_error_after_redirects_is_transient(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    respx.get(f"{REMOTE}/movie/shows").mock(return_value=httpx.Response(502))
    with pytest.raises(TransientError, match="502"):
        await remote_rig(client, fake_clock).check(make_target(listing_url=listing_url(REMOTE)))


@pytest.mark.parametrize("kind", ["captcha", "queue"])
async def test_built_in_checks_spot_interstitials_the_file_does_not_describe(
    kind: str, fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    raw = fakeshow_raw(fakesite.base_url)
    del raw["interstitials"]  # nothing recorded: the built-in title checks still apply (C3)
    rig = Rig(parse_calibration(raw), client, fake_clock)
    fakesite.set(booking_open=True, shows=SHOWS, interstitial=kind)
    with pytest.raises(Blocked) as info:
        await rig.check(target_for(fakesite))
    assert info.value.reason == kind


async def test_today_is_still_checked(
    fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    rig = Rig(fakeshow_calibration(fakesite.base_url), client, fake_clock)
    fakesite.set(booking_open=True, shows=[{**SHOWS[0], "date": "2026-11-03"}])
    snapshot = await rig.check(target_for(fakesite, dates=["2026-11-03"]))  # the clock's today
    assert list(snapshot.open_dates) == [date(2026, 11, 3)]


async def test_dates_added_back_are_read_again_at_once(rig: Rig, fakesite: FakeSite) -> None:
    # Targets can change while ShowWatch runs (remove, then add again). A date that comes back
    # must not be reported from what was read before it was dropped.
    fakesite.set(booking_open=True, shows=SHOWS)
    three = target_for(fakesite, dates=["2026-12-18", "2026-12-19", "2026-12-20"])
    one = target_for(fakesite, dates=["2026-12-18"])
    await rig.check(three)
    await rig.check(one)
    fakesite.reset_log()
    await rig.check(three)
    assert queried(fakesite) == ["date=2026-12-19", "date=2026-12-20"]
