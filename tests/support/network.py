"""Network lockdown for the Python test process (SPEC §9, layer 1).

pytest-socket's `--allow-hosts=127.0.0.1,::1` (set in pyproject.toml) blocks every non-loopback
`connect()`. This module closes the two gaps it leaves open:

* DNS: `--allow-hosts` does not guard name resolution, so a test that forgot its respx mock would
  still send a real DNS query for, say, the Telegram API host. `install_dns_guard` blocks lookups
  of anything except loopback names and IP literals.
* Bypasses: a single `enable_socket` marker, `socket_enabled` fixture, widened `allow_hosts` marker
  or `--force-enable-socket` flag would switch the lockdown off silently. These are rejected.

The browser process is a separate layer (Phase 6, `context.route`).
"""

import ipaddress
import socket
from collections.abc import Callable, Collection
from typing import Any

import pytest
from pytest_socket import SocketBlockedError

LOOPBACK_IPS = frozenset({"127.0.0.1", "::1"})
_LOCAL_NAMES = frozenset({"", "localhost"})  # "" is the wildcard address when binding


class NetworkLockdownError(SocketBlockedError):
    """A test tried to resolve a non-local host name."""


def check_session_config(*, allow_hosts: str | None, force_enabled: bool) -> None:
    """Fail the whole session unless pytest-socket allows loopback IPs and nothing else."""
    if force_enabled:
        raise pytest.UsageError("network lockdown: --force-enable-socket is not allowed")
    hosts = {h.strip() for h in (allow_hosts or "").split(",") if h.strip()}
    if not hosts:
        raise pytest.UsageError(
            "network lockdown is OFF: add --allow-hosts=127.0.0.1,::1 to pytest addopts"
        )
    if not hosts <= LOOPBACK_IPS:
        raise pytest.UsageError(
            f"network lockdown: --allow-hosts may only list {sorted(LOOPBACK_IPS)}, "
            f"got {sorted(hosts)}"
        )


def bypass_problem(
    markers: Collection[str],
    fixtures: Collection[str],
    allow_hosts_marker: str | list[str] | None,
) -> str | None:
    """Describe how a single test would weaken the lockdown, or return None if it would not."""
    if "enable_socket" in markers:
        return "network lockdown: @pytest.mark.enable_socket is not allowed"
    if "socket_enabled" in fixtures:
        return "network lockdown: the socket_enabled fixture is not allowed"
    if allow_hosts_marker is not None:
        hosts = (
            allow_hosts_marker.split(",")
            if isinstance(allow_hosts_marker, str)
            else list(allow_hosts_marker)
        )
        extra = {h.strip() for h in hosts} - LOOPBACK_IPS
        if extra:
            return f"network lockdown: allow_hosts marker widens the allow-list: {sorted(extra)}"
    return None


def _is_local(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if not isinstance(host, str):
        return False
    name = host.strip().lower().rstrip(".")
    if name in _LOCAL_NAMES:
        return True
    try:
        # IP literals resolve without DNS. pytest-socket blocks connect() to non-loopback IPs.
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _guard(real: Callable[..., Any], api: str) -> Callable[..., Any]:
    def guarded(host: object, *args: Any, **kwargs: Any) -> Any:
        if not _is_local(host):
            raise NetworkLockdownError(
                f"A test tried to use socket.{api} for non-local host {host!r} "
                "(network lockdown, SPEC §9). Mock the HTTP call with respx instead."
            )
        return real(host, *args, **kwargs)

    return guarded


def install_dns_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    for api in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, api, _guard(getattr(socket, api), api))
