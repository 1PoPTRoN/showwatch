"""T0.1: the package imports and exposes its version."""

from importlib.metadata import version

import showwatch


def test_t0_1_version_exists() -> None:
    assert isinstance(showwatch.__version__, str)
    assert showwatch.__version__


def test_t0_1_version_matches_installed_metadata() -> None:
    # Guards against the hard-coded version drifting from pyproject.toml.
    assert showwatch.__version__ == version("showwatch")
