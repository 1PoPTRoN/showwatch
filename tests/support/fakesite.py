"""Start, control and inspect the fake site from tests (SPEC §9 "fake-site fixture")."""

import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]


class FakeSite:
    def __init__(self, base_url: str, process: "subprocess.Popen[str]") -> None:
        self.base_url = base_url
        self.process = process

    def url(self, path: str = "/") -> str:
        return f"{self.base_url}{path}"

    def set(self, **state: Any) -> None:
        response = httpx.post(self.url("/__control/state"), json=state, timeout=5)
        if response.status_code != 200:
            raise AssertionError(f"fake site refused {state}: {response.text}")

    def reset(self) -> None:
        httpx.post(self.url("/__control/reset"), timeout=5).raise_for_status()

    def reset_log(self) -> None:
        httpx.post(self.url("/__control/log/clear"), timeout=5).raise_for_status()

    def log(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = httpx.get(self.url("/__control/log"), timeout=5).json()
        return entries


def _stop(process: "subprocess.Popen[str]", base_url: str | None) -> None:
    """Ask for a graceful exit (code 0); fall back to SIGTERM, then SIGKILL."""
    if base_url is not None and process.poll() is None:
        try:
            httpx.post(f"{base_url}/__control/shutdown", timeout=2)
            process.wait(timeout=10)
        except (httpx.TransportError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def run_fakesite(startup_timeout_s: float = 15.0) -> Iterator[FakeSite]:
    """Run the fake site in a subprocess on 127.0.0.1 with a random port; stop it afterwards."""
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.fakesite", "--port", "0"],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        site: FakeSite | None = None
        try:
            assert process.stdout is not None
            line = process.stdout.readline().strip()
            if not line.startswith("FAKESITE_PORT="):
                errors.seek(0)
                raise RuntimeError(f"fake site did not start: {line!r}\n{errors.read()}")
            site = FakeSite(f"http://127.0.0.1:{int(line.split('=', 1)[1])}", process)
            deadline = time.monotonic() + startup_timeout_s
            while True:
                try:
                    if httpx.get(site.url("/__control/health"), timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("fake site did not become healthy in time")
                time.sleep(0.05)
            yield site
        finally:
            _stop(process, site.base_url if site is not None else None)
            if process.stdout is not None:
                process.stdout.close()
