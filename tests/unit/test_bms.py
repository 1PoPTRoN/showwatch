"""Phase 10: BmsAdapter's pure parts: the built-in interstitial titles and the hold countdown."""

import pytest

from showwatch.adapters.bms import builtin_interstitial, countdown_seconds


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Verify you are human · Cinema", "captcha"),
        ("Please verify that you're a human", "captcha"),
        ("Security check: CAPTCHA", "captcha"),
        ("Are you a robot?", "captcha"),
        ("You are now in the queue", "queue"),
        ("You're in line", "queue"),
        ("Virtual waiting room", "queue"),
        ("Access denied", "forbidden"),
        ("403 Forbidden", "forbidden"),
        ("Forbidden", "forbidden"),
    ],
)
def test_builtin_titles_spot_interstitials(title: str, kind: str) -> None:
    assert builtin_interstitial(title) == kind


@pytest.mark.parametrize(
    "title",
    [
        "Test Movie · Book tickets",
        "The Queue (2026) · Book tickets",  # a movie title is part of most page titles
        "Waiting Room · Showtimes",
        "Forbidden Kingdom · Book tickets",
        "Robots · Showtimes",
        "",
    ],
)
def test_builtin_titles_leave_ordinary_pages_alone(title: str) -> None:
    assert builtin_interstitial(title) is None


@pytest.mark.parametrize(
    ("iso", "text", "seconds"),
    [
        ("PT600S", "", 600),
        ("PT9M58S", "ignored 00:01", 598),
        ("PT1H", "", 3600),
        ("PT1H2M3.5S", "", 3723),
        (None, "Complete your booking in 09:58", 598),
        (None, "1:02:03 left", 3723),
        ("not a duration", "07:00", 420),
        ("PT", "05:00", 300),  # an empty duration says nothing; the text does
        (None, "seat 12345", None),
        (None, "", None),
    ],
)
def test_hold_countdowns(iso: str | None, text: str, seconds: int | None) -> None:
    assert countdown_seconds(iso, text) == seconds
