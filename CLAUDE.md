# ShowWatch — working notes for Claude Code
Full spec: `docs/SPEC.md` (source of truth). Risks and proposed resolutions: `docs/DESIGN-NOTES.md`.
Personal BookMyShow watcher: detects openings, ranks shows/seats, holds seats, hands payment to the human.

## Working rules
- Build phase by phase (SPEC §10): restate goal + files → write the phase's tests first and see them fail
  for the right reason → implement → `make check` green → update README.md (progress table, anything new
  to run) → phase report (§11) → commit `phase N: <summary>` on branch `showwatch-build` → fast-forward
  `main` to it (`git merge --ff-only`) → STOP and wait for "continue". Ask before any `git push`.
- Tests are the contract: never weaken, skip, xfail or delete one. If a test looks wrong, say so in the report.
- Ask first before: adding a dependency not in §4, changing a §7 interface, or changing anything in §2.
- NEVER contact a real site (bookmyshow.com or any other) from tests or tool runs. Mocked HTTP + local fake site only.
- If anything needs to get around bot detection, a CAPTCHA, a queue or rate limits: STOP and ask.

## Hard constraints (§2, non-negotiable)
- C1 No payment automation. Executor stops at the order summary. Every click goes through `safe_click()`, which
  raises `PaymentBoundaryError` on the payment page or a forbidden locator. Never click "continue to payment".
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
- Dev: pytest, pytest-asyncio, respx, hypothesis, pytest-socket, ruff, mypy, types-PyYAML, starlette, uvicorn.
- Storage: stdlib sqlite3 (WAL) behind `Store`, numbered migrations. Scheduling: own asyncio loop on `Clock`.

## Conventions (§5)
- `mypy --strict` passes on `src/`. All I/O is async. `matcher/` and `watcher/diff.py` are pure (no I/O, no clock).
- Time only through `Clock`: `datetime.now(`, `utcnow(`, `today(`, `time.time(`, `time.sleep(` and
  `asyncio.sleep(` are banned outside `clock.py` (enforced by tests/unit/test_architecture.py).
- Inject every side effect: Clock, SiteAdapter / injected httpx client, Store.
- C4/C5 numbers live only in `limits.py` (pinned by test_limits.py); import them, never repeat literals.
- User config enters only via `config.parse_settings*` / `load_settings` (secret + past-date checks).
- Store UTC; show users `Asia/Kolkata` via `zoneinfo`.
- Logs: JSON lines in `~/.showwatch/logs/`, through the redaction filter. Artefacts: `~/.showwatch/runs/<attempt_id>/NN-<step>.png`.
- Locators: `get_by_role` / `get_by_text` / `get_by_test_id` only; no deep CSS or XPath.
- Typed exceptions (§7); no bare `except:`. Pydantic models use `extra="forbid"`; secrets are never read from YAML.
- Tests: `tests/{unit,integration,e2e}` are auto-marked by folder. Names carry spec ids: `test_t4_7_...`.
  The network lockdown (pytest-socket, `127.0.0.1`/`::1` only) is always on; never disable it.

## Commands (§6)
- `make check`: ruff check, ruff format --check, mypy --strict src, mypy tests, pytest -q (all markers). Must be green.
- `make test`: pytest only.  `make e2e`: pytest -m e2e.  `make fakesite`: run the fake site (Phase 6+).
- `make fmt`: auto-format and apply safe lint fixes.
- CLI: `uv run showwatch --help` (login, add, list, status, run, pause, resume, done, cancel, heartbeat, canary, parse).
