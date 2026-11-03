"""Shared pytest configuration and fixtures.

* Network lockdown (SPEC §9): loopback-only connects (pytest-socket), plus DNS and bypass guards
  from tests/support/network.py. It is always on and no test can switch it off.
* Layers: each test is marked unit / integration / e2e from its folder, so `-m` selects reliably.
* `fake_clock`: virtual time (tests/support/clock.py).

Later phases add the tmp store and fake-site fixtures here.
"""

from pathlib import Path

import pytest

from tests.support import network
from tests.support.clock import FakeClock

TESTS_ROOT = Path(__file__).resolve().parent
LAYERS = ("unit", "integration", "e2e")


def pytest_configure(config: pytest.Config) -> None:
    network.check_session_config(
        allow_hosts=config.getoption("--allow-hosts"),
        force_enabled=config.getoption("--force-enable-socket"),
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    problems: list[str] = []
    for item in items:
        layer = _layer_of(item)
        if layer is None:
            problems.append(f"{item.nodeid}: tests must live under tests/{{{','.join(LAYERS)}}}/")
            continue
        item.add_marker(layer)
        allow = item.get_closest_marker("allow_hosts")
        problem = network.bypass_problem(
            markers={marker.name for marker in item.iter_markers()},
            fixtures=getattr(item, "fixturenames", ()),
            allow_hosts_marker=allow.args[0] if allow and allow.args else None,
        )
        if problem:
            problems.append(f"{item.nodeid}: {problem}")
    if problems:
        raise pytest.UsageError("\n".join(problems))


def _layer_of(item: pytest.Item) -> str | None:
    try:
        first = item.path.resolve().relative_to(TESTS_ROOT).parts[0]
    except ValueError:
        return None
    return first if first in LAYERS else None


@pytest.fixture(autouse=True)
def _dns_lockdown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Function-scoped on purpose: pytest-socket's per-test teardown would drop a session-wide patch.
    network.install_dns_guard(monkeypatch)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
