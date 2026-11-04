"""The FakeShow calibration (tests/support/fakeshow-calibration.yaml) for a running FakeShow."""

from pathlib import Path
from typing import Any

import yaml

from showwatch.adapters.calibration import Calibration, parse_calibration

TEMPLATE = Path(__file__).with_name("fakeshow-calibration.yaml")


def fakeshow_raw(base_url: str) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(TEMPLATE.read_text().replace("{base}", base_url))
    return raw


def fakeshow_calibration(base_url: str) -> Calibration:
    return parse_calibration(fakeshow_raw(base_url))


def listing_url(base_url: str) -> str:
    """FakeShow's per-date listing, as a target's `listing_url` template."""
    return f"{base_url}/movie/shows?date={{date:%Y-%m-%d}}"
