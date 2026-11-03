"""T0.3: the Python-process network lockdown works (SPEC §9, layer 1).

Only RFC 5737 / RFC 3849 documentation addresses and the reserved `.invalid` TLD are used, so
even a broken lockdown could not reach a real host. Every test first checks that the lockdown is
configured, so a missing lockdown fails fast instead of attempting a connection, and a timeout is
never mistaken for a block.
"""

import asyncio
import socket
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from pytest_socket import SocketBlockedError, SocketConnectBlockedError

from tests.support import network as lockdown

NON_LOCAL_ADDRESSES = [
    pytest.param(("203.0.113.10", 80), id="ipv4-test-net-3"),
    pytest.param(("2001:db8::10", 80), id="ipv6-documentation"),
]
UNRESOLVABLE_HOST = "showwatch-lockdown-check.invalid"


@pytest.fixture(autouse=True)
def _require_lockdown(pytestconfig: pytest.Config) -> None:
    allowed = pytestconfig.getoption("--allow-hosts")
    assert allowed, "pytest-socket --allow-hosts is not configured: the network lockdown is OFF"


@contextmanager
def _expect_blocked(exc_type: type[BaseException]) -> Iterator[None]:
    # pytest-socket also emits a UserWarning when it blocks. Outside this helper that warning is
    # an error (filterwarnings=error), so any accidental network use fails loudly.
    with pytest.raises(exc_type), pytest.warns(UserWarning, match="tried to use socket"):
        yield


def test_t0_3_lockdown_allows_loopback_only(pytestconfig: pytest.Config) -> None:
    hosts = sorted(h.strip() for h in pytestconfig.getoption("--allow-hosts").split(","))
    assert hosts == ["127.0.0.1", "::1"]
    assert not pytestconfig.getoption("--force-enable-socket")


@pytest.mark.parametrize("address", NON_LOCAL_ADDRESSES)
def test_t0_3_blocks_non_local_connect(address: tuple[str, int]) -> None:
    with _expect_blocked(SocketConnectBlockedError):
        socket.create_connection(address, timeout=1)


async def test_t0_3_blocks_non_local_asyncio_connect() -> None:
    with _expect_blocked(SocketConnectBlockedError):
        await asyncio.open_connection("203.0.113.10", 80)


def test_t0_3_blocks_dns_for_non_local_hosts() -> None:
    with _expect_blocked(SocketBlockedError):
        socket.getaddrinfo(UNRESOLVABLE_HOST, 443)
    with _expect_blocked(SocketBlockedError):
        socket.gethostbyname(UNRESOLVABLE_HOST)


async def test_t0_3_blocks_async_dns_for_non_local_hosts() -> None:
    with _expect_blocked(SocketBlockedError):
        await asyncio.get_running_loop().getaddrinfo(UNRESOLVABLE_HOST, 443)


async def test_t0_3_blocks_httpx_without_a_mock() -> None:
    # The common failure: a test forgets its respx mock. It must not leak even a DNS query.
    async with httpx.AsyncClient() as client:
        with _expect_blocked(SocketBlockedError):
            await client.get(f"https://{UNRESOLVABLE_HOST}/")


def test_t0_3_loopback_still_works() -> None:
    # Positive control: the fake site (Phase 6) runs on 127.0.0.1 and must stay reachable.
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    assert socket.getaddrinfo("localhost", 80)


# --- the guards that stop a test from switching the lockdown off -------------------------------


@pytest.mark.parametrize(
    ("allow_hosts", "force_enabled"),
    [
        pytest.param(None, False, id="lockdown-missing"),
        pytest.param("", False, id="empty-allow-list"),
        pytest.param("127.0.0.1,203.0.113.10", False, id="non-loopback-host"),
        pytest.param("127.0.0.0/8", False, id="cidr-network"),
        pytest.param("localhost", False, id="hostname-instead-of-ip"),
        pytest.param("127.0.0.1,::1", True, id="force-enable-socket"),
    ],
)
def test_t0_3_session_config_rejects_weakened_lockdown(
    allow_hosts: str | None, force_enabled: bool
) -> None:
    with pytest.raises(pytest.UsageError, match="lockdown"):
        lockdown.check_session_config(allow_hosts=allow_hosts, force_enabled=force_enabled)


@pytest.mark.parametrize("allow_hosts", ["127.0.0.1,::1", "127.0.0.1", " ::1 , 127.0.0.1 "])
def test_t0_3_session_config_accepts_loopback(allow_hosts: str) -> None:
    lockdown.check_session_config(allow_hosts=allow_hosts, force_enabled=False)


@pytest.mark.parametrize(
    ("markers", "fixtures", "allow_hosts_marker"),
    [
        pytest.param({"enable_socket"}, set(), None, id="enable_socket-marker"),
        pytest.param(set(), {"socket_enabled"}, None, id="socket_enabled-fixture"),
        pytest.param({"allow_hosts"}, set(), ["203.0.113.10"], id="allow_hosts-widened"),
        pytest.param({"allow_hosts"}, set(), "127.0.0.1,198.51.100.7", id="allow_hosts-csv"),
    ],
)
def test_t0_3_item_bypass_is_detected(
    markers: set[str], fixtures: set[str], allow_hosts_marker: list[str] | str | None
) -> None:
    problem = lockdown.bypass_problem(markers, fixtures, allow_hosts_marker)
    assert problem is not None
    assert "lockdown" in problem


@pytest.mark.parametrize(
    ("markers", "fixtures", "allow_hosts_marker"),
    [
        pytest.param(set(), set(), None, id="plain-test"),
        pytest.param({"disable_socket"}, {"socket_disabled"}, None, id="stricter-is-fine"),
        pytest.param({"allow_hosts"}, set(), ["127.0.0.1"], id="allow_hosts-loopback"),
    ],
)
def test_t0_3_item_without_bypass_is_accepted(
    markers: set[str], fixtures: set[str], allow_hosts_marker: list[str] | str | None
) -> None:
    assert lockdown.bypass_problem(markers, fixtures, allow_hosts_marker) is None
