"""One ShowWatch process at a time for anything that talks to the site or uses the browser profile.

Two `showwatch run` processes would each keep their own 20 s spacing (breaking C4) and fight over
Chromium's profile lock. `login` and `canary` take the same lock. The lock is an advisory
`flock` on a private file, released automatically if the process dies.
"""

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self

from showwatch.errors import AlreadyRunning


class InstanceLock:
    def __init__(self, path: Path, *, purpose: str) -> None:
        self._path = path
        self._purpose = purpose
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(
                f"ShowWatch is already running elsewhere (needed for {self._purpose}); "
                "stop it first"
            ) from None
        os.chmod(self._path, 0o600)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def is_held_elsewhere(self) -> bool:
        try:
            self.acquire()
        except AlreadyRunning:
            return True
        self.release()
        return False

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
