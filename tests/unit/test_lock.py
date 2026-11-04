"""Phase 8: one `showwatch run` per machine (docs/DESIGN-NOTES.md X3).

Two runs would each keep their own 20 s spacing (breaking C4) and fight over the browser profile.
"""

from pathlib import Path

import pytest

from showwatch.errors import AlreadyRunning
from showwatch.lock import InstanceLock


def test_a_second_holder_is_refused_until_the_first_lets_go(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with InstanceLock(path, purpose="run"):
        with pytest.raises(AlreadyRunning, match="run"):
            InstanceLock(path, purpose="login").acquire()
        assert InstanceLock(path, purpose="check").is_held_elsewhere()
    with InstanceLock(path, purpose="run again"):
        pass
    assert not InstanceLock(path, purpose="check").is_held_elsewhere()


def test_the_lock_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with InstanceLock(path, purpose="run"):
        assert path.stat().st_mode & 0o077 == 0
