# ShowWatch — working notes for Claude Code
Full spec: `docs/SPEC.md` (source of truth). Risks and proposed resolutions: `docs/DESIGN-NOTES.md`.
Personal BookMyShow watcher: detects openings, ranks shows/seats, holds seats, hands payment to the human.

## Working rules
- Build phase by phase (SPEC §10): restate goal + files → write the phase's tests first and see them fail
  for the right reason → implement → `make check` green → update README.md (progress table, anything new
  to run) → phase report (§11) → commit `phase N: <summary>` on branch `showwatch-build` → fast-forward
  `main` to it (`git merge --ff-only`) → `git push origin main` (standing approval from the user) → STOP
  and wait for "continue". The repo is public: never push anything else without asking.
- Coverage for the report (no project dependency added):
  `uv run --locked --with coverage coverage run --branch --source=src/showwatch -m pytest`.
- Tests are the contract: never weaken, skip, xfail or delete one. If a test looks wrong, say so in the report.
- Gate commit/push on `make check`'s own exit code (`make check && git ...`), never on a piped grep.
- Ask first before: adding a dependency not in §4, changing a §7 interface, or changing anything in §2.
- NEVER contact a real site (bookmyshow.com or any other) from tests or tool runs. Mocked HTTP + local fake site only.
- If anything needs to get around bot detection, a CAPTCHA, a queue or rate limits: STOP and ask.

## Hard constraints (§2, non-negotiable)
- C1 No payment automation. Executor stops at the order summary. Clicks only via `executor/guards.py`
  (`safe_click`/`safe_select`); no typing API anywhere (test_architecture enforces both).
- C2 No credentials. User logs in manually (`showwatch login`, headed). Never read/store/log/type passwords or OTPs.
- C3 No evasion: no CAPTCHA solving, stealth plugins, fingerprint/UA spoofing, proxies or multiple accounts.
  CAPTCHA, queue or block page → PAUSED, leave the browser as it is, notify, take no further action on that target.
- C4 Polite traffic: at most 1 request per 20 s globally; per-target floor of 60 s (hard-coded, config can't lower it);
  single-flight; exponential backoff on 429/5xx; 403 → pause + notify.
- C5 Personal scale: 1 account, at most 10 seats per target, at most 3 targets. No resale or bulk features.
- C6 Executor browser is headed by default; headless only in tests.
- C7 Secrets only in env vars (`.env` is gitignored). Logs redact cookies, tokens, the profile path and
  token-bearing URLs (the Telegram Bot API URL contains the token, and httpx logs URLs at INFO).

## Stack (§4)
- Python 3.12, `uv` (env + lockfile), src layout, package `showwatch`.
- Runtime: playwright, httpx, pydantic>=2, pyyaml, typer, python-dotenv (+ anthropic in Phase 9).
- Dev: pytest(-asyncio,-socket,-timeout), respx, hypothesis, ruff, mypy, types-PyYAML, starlette, uvicorn.
- Storage: stdlib sqlite3 (WAL) behind `Store`, numbered migrations. Scheduling: own asyncio loop on `Clock`.

## Conventions (§5)
- `mypy --strict` passes on `src/`. All I/O is async. `matcher/` and `watcher/diff.py` are pure (no I/O, no clock).
- Time only through `Clock`: `datetime.now(`, `utcnow(`, `today(`, `time.time(`, `time.sleep(` and
  `asyncio.sleep(` are banned outside `clock.py` (enforced by tests/unit/test_architecture.py).
- Inject every side effect: Clock, SiteAdapter / injected httpx client, Store.
- C4/C5 numbers live only in `limits.py` (pinned by test_limits.py); import them, never repeat literals.
- User config enters only via `config.parse_settings*` / `load_settings` (secret + past-date checks).
- Every availability check goes through `PollGate.poll()`; every poll result through `Store.record_poll()`.
- State changes only through `Store.fire()` (one transaction: state + attempt + audit). Never UPDATE
  `targets.state` directly. Schema changes = append a migration to `store.MIGRATIONS`, never edit one.
- Store UTC; show users `Asia/Kolkata`. Artefacts: `~/.showwatch/runs/<attempt_id>/NN-<step>.png`.
- Logs: `showwatch.logging.configure_logging` (JSON lines, redacted when each record is created). New secret
  (token, profile path)? `register_secret()` it. Never log raw httpx exceptions (their text holds the URL).
- The pipeline notifies only through `DedupNotifier` (at most once per event id, never raises).
- Locators: `get_by_role` / `get_by_text` / `get_by_test_id` only; no deep CSS or XPath.
- Typed exceptions (§7); no bare `except:`. Pydantic models use `extra="forbid"`; secrets are never read from YAML.
- Tests: `tests/{unit,integration,e2e}` are auto-marked by folder. Names carry spec ids: `test_t4_7_...`.
  The network lockdown (pytest-socket, `127.0.0.1`/`::1` only) is always on; never disable it.

## Commands (§6)
- `make check`: ruff check, ruff format --check, mypy --strict src, mypy tests, pytest -q (all markers). Must be green.
- `make test`, `make e2e`, `make fakesite` (FakeShow on 127.0.0.1), `make fmt`. Browser: `playwright install chromium`.
- CLI: `uv run showwatch --help` (§6 commands + `remove`). Data: `$SHOWWATCH_HOME` (default `~/.showwatch`).
