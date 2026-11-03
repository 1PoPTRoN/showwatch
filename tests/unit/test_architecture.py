"""T0.5: architecture rule. Time only goes through `Clock` (SPEC §5).

Two scans of `src/`, with `clock.py` the only exemption:
  * the literal scan required by the spec: no `datetime.now(` or `time.time(`;
  * an AST scan that also catches aliases (`from time import time as t`), the other wall-clock
    reads (`utcnow`, `today`, `monotonic`, `perf_counter`) and real sleeps (`time.sleep`,
    `asyncio.sleep`). Any of these would bypass FakeClock, making tests slow or nondeterministic.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "showwatch"
CLOCK_MODULE = SRC / "clock.py"

LITERAL_BANS = ("datetime.now(", "time.time(")
BANNED_NAMES = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.sleep",
        "asyncio.sleep",
    }
)


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Map each locally bound name to the dotted module path it refers to."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def find_clock_violations(source: str, filename: str = "<snippet>") -> list[str]:
    tree = ast.parse(source, filename)
    aliases = _aliases(tree)
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        # Any reference counts, not just a call: `stamp = time.monotonic` is as bad as calling it.
        if isinstance(node, ast.Name | ast.Attribute):
            name = _dotted(node, aliases)
            if name in BANNED_NAMES:
                found.add((node.lineno, f"{name} (via `{ast.unparse(node)}`)"))
    for lineno, line in enumerate(source.splitlines(), start=1):
        for ban in LITERAL_BANS:
            if ban in line:
                found.add((lineno, f"literal {ban!r}"))
    return [f"{filename}:{lineno}: {what}" for lineno, what in sorted(found)]


def _scanned_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p != CLOCK_MODULE)


# --- the rule itself ---------------------------------------------------------------------------


def test_t0_5_src_reads_time_only_through_clock() -> None:
    violations = [
        v
        for path in _scanned_files()
        for v in find_clock_violations(path.read_text(), str(path.relative_to(SRC.parent)))
    ]
    assert violations == [], "Use the injected Clock instead:\n" + "\n".join(violations)


def test_t0_5_scan_is_not_vacuous() -> None:
    names = {p.relative_to(SRC).as_posix() for p in _scanned_files()}
    assert {"__init__.py", "cli.py"} <= names, names


def test_t0_5_clock_module_is_the_exemption_and_really_reads_time() -> None:
    # Proves two things: the exemption names a real file, and the checker sees real time calls.
    assert CLOCK_MODULE.is_file()
    assert find_clock_violations(CLOCK_MODULE.read_text()), "checker found nothing in clock.py"


# --- the checker catches every spelling ------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "from datetime import datetime\ndatetime.now()",
        "import datetime\ndatetime.datetime.now(datetime.UTC)",
        "import datetime as dt\ndt.datetime.utcnow()",
        "from datetime import date\ndate.today()",
        "from datetime import datetime as DT\nDT.now()",
        "import time\ntime.time()",
        "from time import time as wall\nwall()",
        "import time\nstamp = time.monotonic",
        "from time import perf_counter\nperf_counter()",
        "import time\ntime.sleep(1)",
        "import asyncio\nasync def f() -> None:\n    await asyncio.sleep(1)",
        "from asyncio import sleep\nasync def f() -> None:\n    await sleep(1)",
    ],
)
def test_t0_5_checker_catches(snippet: str) -> None:
    assert find_clock_violations(snippet), f"not caught:\n{snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        "async def f(clock) -> None:\n    clock.now()\n    await clock.sleep(5)",
        "from datetime import UTC, datetime\nx = datetime(2026, 1, 1, tzinfo=UTC)",
        "from datetime import timedelta\ny = timedelta(seconds=60)",
        "def sleep(s: float) -> None: ...\nsleep(1)",
    ],
)
def test_t0_5_checker_allows(snippet: str) -> None:
    assert find_clock_violations(snippet) == []


# --- pure modules (SPEC §5): matcher/ and watcher/diff.py do no I/O and read no clock ----------

PURE_PATHS = (SRC / "matcher", SRC / "watcher" / "diff.py")
PURE_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "itertools",
        "math",
        "operator",
        "typing",
        "zoneinfo",
        "showwatch.config",
        "showwatch.limits",
        "showwatch.models",
        "showwatch.matcher",
        "showwatch.watcher.diff",
    }
)


def _allowed(name: str) -> bool:
    return any(name == ok or name.startswith(f"{ok}.") for ok in PURE_ALLOWED_IMPORTS)


def find_impure_imports(source: str, module: str, is_package: bool = False) -> list[str]:
    package = module if is_package else module.rpartition(".")[0]
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if not _allowed(a.name)]
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1]) if node.level else ""
            target = ".".join(p for p in (base, node.module or "") if p)
            found += [
                f"from {target} import {a.name}"
                for a in node.names
                if not (_allowed(target) or _allowed(f"{target}.{a.name}"))
            ]
    return found


def _pure_files() -> list[Path]:
    files: list[Path] = []
    for path in PURE_PATHS:
        files += sorted(path.rglob("*.py")) if path.is_dir() else [path] if path.is_file() else []
    return files


def test_pure_modules_import_nothing_that_does_io() -> None:
    files = _pure_files()
    assert files, "no pure modules found: has matcher/ moved?"
    violations = []
    for path in files:
        module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
        is_package = path.name == "__init__.py"
        module = module.removesuffix(".__init__")
        violations += [
            f"{path.name}: {v}" for v in find_impure_imports(path.read_text(), module, is_package)
        ]
    assert violations == [], "pure modules must not import I/O:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "snippet",
    [
        "import asyncio",
        "import httpx",
        "import os",
        "from pathlib import Path",
        "from showwatch.clock import Clock",
        "from showwatch import clock",
        "from ..store import Store",
        "from .. import executor",
    ],
)
def test_purity_checker_catches(snippet: str) -> None:
    assert find_impure_imports(snippet, "showwatch.matcher.seats"), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "from collections.abc import Sequence",
        "from showwatch.models import SeatMap",
        "from showwatch import models",
        "from .shows import rank_shows",
        "from . import seats",  # a sibling inside matcher/, itself covered by the scan
        "from zoneinfo import ZoneInfo",
    ],
)
def test_purity_checker_allows(snippet: str) -> None:
    assert find_impure_imports(snippet, "showwatch.matcher.seats") == [], snippet
