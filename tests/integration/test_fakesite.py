"""Phase 6: the local fake site ("FakeShow") and FakeSiteAdapter (tests T6.1-T6.7).

The site runs as a uvicorn subprocess on 127.0.0.1 with a random port, shared by the session and
reset before each test. Every request here is to 127.0.0.1; the network lockdown still applies.
"""

import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from showwatch.adapters.fake import FakeSiteAdapter
from showwatch.errors import Blocked, RateLimited, TransientError
from tests.support.clock import FakeClock
from tests.support.fakesite import FakeSite, run_fakesite
from tests.support.targets import make_target

FAKESITE_DIR = Path(__file__).resolve().parents[1] / "fakesite"
SHOWS = [
    {
        "show_id": "fri-a-19",
        "venue": "Cinema A",
        "date": "2026-12-18",
        "time": "19:00",
        "format": "IMAX 2D",
        "min_price": 450,
    },
    {
        "show_id": "fri-b-21",
        "venue": "Cinema B",
        "date": "2026-12-18",
        "time": "21:15",
        "format": "2D",
        "min_price": 250,
    },
    {
        "show_id": "sat-a-18",
        "venue": "Cinema A",
        "date": "2026-12-19",
        "time": "18:30",
        "format": "4DX",
        "min_price": 600,
    },
]


def adapter(site: FakeSite, client: httpx.AsyncClient, clock: FakeClock) -> FakeSiteAdapter:
    return FakeSiteAdapter(site.base_url, client=client, clock=clock)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as http:
        yield http


# --- T6.1 ------------------------------------------------------------------------------------


def test_t6_1_starts_on_loopback_with_a_random_port_and_stops_cleanly() -> None:
    with run_fakesite() as first, run_fakesite() as second:
        for site in (first, second):
            assert re.fullmatch(r"http://127\.0\.0\.1:\d+", site.base_url)
            assert httpx.get(site.url("/__control/health")).json() == {"ok": True}
        assert first.base_url != second.base_url  # random ports, never a fixed one
        process = first.process
    assert process.returncode == 0  # clean shutdown
    with pytest.raises(httpx.ConnectError):
        httpx.get(first.url("/__control/health"), timeout=1)


# --- T6.2 ------------------------------------------------------------------------------------


def test_t6_2_control_api_flips_booking_open(fakesite: FakeSite) -> None:
    page = httpx.get(fakesite.url("/movie")).text
    assert "Coming soon" in page
    assert "Book tickets" not in page

    fakesite.set(booking_open=True, movie="Monsoon Nights")
    page = httpx.get(fakesite.url("/movie")).text
    assert "Book tickets" in page
    assert "Monsoon Nights" in page


def test_unknown_control_keys_are_rejected(fakesite: FakeSite) -> None:
    response = httpx.post(fakesite.url("/__control/state"), json={"bookng_open": True})
    assert response.status_code == 400
    assert "bookng_open" in response.text


# --- T6.3 ------------------------------------------------------------------------------------


async def test_t6_3_availability_matches_the_configured_shows(
    fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    target = make_target(dates=["2026-12-18", "2026-12-19"])

    snapshot = await adapter(fakesite, client, fake_clock).check_availability(target)

    assert snapshot.target_id == target.id
    assert snapshot.taken_at == fake_clock.now()
    assert sorted(snapshot.open_dates) == [date(2026, 12, 18), date(2026, 12, 19)]
    fri = {s.show_id: s for s in snapshot.open_dates[date(2026, 12, 18)]}
    assert set(fri) == {"fri-a-19", "fri-b-21"}
    a19 = fri["fri-a-19"]
    assert (a19.venue, a19.screen_format, a19.language, a19.min_price) == (
        "Cinema A",
        "IMAX 2D",
        "hindi",
        450,
    )
    assert a19.starts_at == datetime(2026, 12, 18, 13, 30, tzinfo=UTC)  # 19:00 IST
    assert a19.url == fakesite.url("/show/fri-a-19")


async def test_availability_only_reports_the_targets_dates(
    fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    snapshot = await adapter(fakesite, client, fake_clock).check_availability(make_target())
    assert list(snapshot.open_dates) == [date(2026, 12, 18)]


async def test_closed_booking_means_no_open_dates(
    fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    fakesite.set(booking_open=False, shows=SHOWS)
    snapshot = await adapter(fakesite, client, fake_clock).check_availability(make_target())
    assert dict(snapshot.open_dates) == {}


async def test_one_availability_check_is_exactly_one_request(
    fakesite: FakeSite, client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    # C4 spaces *requests*; an adapter that fanned out per date would break it silently.
    fakesite.set(booking_open=True, shows=SHOWS)
    await adapter(fakesite, client, fake_clock).check_availability(
        make_target(dates=["2026-12-18", "2026-12-19"])
    )
    assert len(fakesite.log()) == 1


# --- T6.4 ------------------------------------------------------------------------------------


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
async def test_t6_4_site_trouble_maps_to_typed_errors(
    state: dict[str, object],
    error: type[Exception],
    reason: str | None,
    says: str,
    fakesite: FakeSite,
    client: httpx.AsyncClient,
    fake_clock: FakeClock,
) -> None:
    # The message is checked too, so the right error type cannot be raised for the wrong reason.
    fakesite.set(booking_open=True, shows=SHOWS, **state)
    with pytest.raises(error, match=says) as info:
        await adapter(fakesite, client, fake_clock).check_availability(make_target())
    if reason is not None:
        assert isinstance(info.value, Blocked)
        assert info.value.reason == reason


async def test_unreachable_site_is_transient(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    with run_fakesite() as site:
        base = site.base_url
    gone = FakeSiteAdapter(base, client=client, clock=fake_clock)
    with pytest.raises(TransientError, match="ConnectError"):
        await gone.check_availability(make_target())


@pytest.mark.parametrize(
    "url", ["https://in.example.com", "http://203.0.113.10:8000", "http://localhost.evil:80"]
)
def test_the_fake_adapter_only_ever_talks_to_loopback(url: str, fake_clock: FakeClock) -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        FakeSiteAdapter(url, client=httpx.AsyncClient(), clock=fake_clock)


# --- T6.5 / T6.6 -----------------------------------------------------------------------------


def test_t6_5_every_site_request_is_logged_in_order(fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    httpx.get(fakesite.url("/movie"))
    httpx.get(fakesite.url("/movie/shows"))
    httpx.post(
        fakesite.url("/show/fri-a-19/select"), data={"qty": "2", "selected": "", "seat": "H5"}
    )
    log = fakesite.log()
    assert [(e["method"], e["path"]) for e in log] == [
        ("GET", "/movie"),
        ("GET", "/movie/shows"),
        ("POST", "/show/fri-a-19/select"),
    ]
    assert all(e["at"].endswith("+00:00") for e in log)
    assert all("/__control" not in e["path"] for e in log)  # the control API is not site traffic


def test_t6_6_payment_posts_are_logged(fakesite: FakeSite) -> None:
    httpx.post(fakesite.url("/pay"), data={"hold_id": "nope"})
    assert [(e["method"], e["path"]) for e in fakesite.log()] == [("POST", "/pay")]


# --- T6.7 ------------------------------------------------------------------------------------


def test_t6_7_the_fake_site_carries_no_third_party_branding() -> None:
    templates = sorted((FAKESITE_DIR / "templates").glob("*.html"))
    assert templates, "no templates found"
    everything = [
        p for p in FAKESITE_DIR.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    ]
    for path in everything:
        text = path.read_text(errors="replace").lower()
        assert "bookmyshow" not in text, path
        assert "bms" not in text, path
    for path in templates:
        text = path.read_text().lower()
        assert "<img" not in text, path
        assert "logo" not in text, path
        assert "http://" not in text, path  # no external assets
        assert "https://" not in text, path
    images = [
        p
        for p in everything
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
    ]
    assert images == []


# --- the booking flow the executor will drive (Phase 7) ---------------------------------------


def seat_page(site: FakeSite, show_id: str = "fri-a-19", qty: int = 2, selected: str = "") -> str:
    return httpx.get(
        site.url(f"/show/{show_id}/seats"), params={"qty": qty, "selected": selected}
    ).text


def select(site: FakeSite, seat: str, selected: str = "", qty: int = 2) -> httpx.Response:
    return httpx.post(
        site.url("/show/fri-a-19/select"),
        data={"qty": str(qty), "selected": selected, "seat": seat},
        follow_redirects=True,
    )


def test_seat_map_shows_rows_gaps_and_taken_seats(fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS, seat_layout=["A: AA_AA", "B: AAAAA"], taken=["A2"])
    page = seat_page(fakesite)
    assert 'role="grid"' in page
    assert re.search(r'data-row="A" data-col="2"[^>]*data-gap="true"', page)
    assert re.search(r'aria-label="A2"[^>]*disabled', page)  # taken
    assert re.search(
        r'aria-label="A3"[^>]*aria-pressed="false"', page
    )  # A3 is the cell after the gap


def test_selecting_a_seat_presses_it(fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS)
    page = select(fakesite, "H5").text
    assert re.search(r'aria-label="H5"[^>]*aria-pressed="true"', page)


def test_steal_on_click_takes_the_seat_instead(fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS, steal_seats_on_click=1)
    lost = select(fakesite, "H5").text
    assert "just taken" in lost
    assert re.search(r'aria-label="H5"[^>]*disabled', lost)
    won = select(fakesite, "H6").text  # only the first click is stolen
    assert re.search(r'aria-label="H6"[^>]*aria-pressed="true"', won)


def test_holding_seats_shows_the_summary_with_a_countdown(fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS, hold_seconds=300)
    response = httpx.post(
        fakesite.url("/show/fri-a-19/hold"),
        data={"qty": "2", "selected": "H5,H6"},
        follow_redirects=True,
    )
    page = response.text
    assert "Order summary" in page
    assert "H5, H6" in page
    assert re.search(r'<time datetime="PT(29\d|300)S">0[45]:\d\d</time>', page)
    assert "Continue to payment" in page
    assert re.search(r'aria-label="H5"[^>]*disabled', seat_page(fakesite))  # held seats are taken


def test_renamed_locator_changes_the_button(fakesite: FakeSite) -> None:
    fakesite.set(booking_open=True, shows=SHOWS, rename_locator={"proceed_to_summary": "Book now"})
    page = seat_page(fakesite, selected="H5,H6")
    assert "Book now" in page
    assert "Pay ₹" not in page


@respx.mock
async def test_a_page_that_says_coming_soon_never_yields_shows(
    client: httpx.AsyncClient, fake_clock: FakeClock
) -> None:
    # A real "coming soon" page may still carry show-like links (for example "notify me").
    # Booking is open only when the page says so. respx serves the page: no server is involved.
    soon = (
        '<main data-page="listing" data-booking="soon"><a href="/show/x" data-show-id="x" '
        'data-venue="Cinema A" data-format="2D" data-language="hindi" '
        'data-starts-at="2026-12-18T19:00:00+05:30" data-min-price="300">x</a></main>'
    )
    respx.get("http://127.0.0.1:9/movie/shows").mock(return_value=httpx.Response(200, text=soon))
    adapter = FakeSiteAdapter("http://127.0.0.1:9", client=client, clock=fake_clock)
    assert dict((await adapter.check_availability(make_target())).open_dates) == {}
