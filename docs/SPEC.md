# ShowWatch — build brief for Claude Code

> **For the human:** put this file in an empty repo and tell Claude Code:
> `Read showwatch-claude-code-brief.md in full, then start Phase 0.`

---

## 0. How you (Claude Code) must work on this project

You are building **ShowWatch**, a personal assistant for a single user. It watches BookMyShow until booking opens for a chosen movie, then picks the best show and seats according to the user's rules. It holds those seats in the user's own logged-in browser and hands the payment step to the human.

### Working rules

1. **Set up the docs first.** Before writing any code:
   - save this brief verbatim as `docs/SPEC.md`;
   - create a `CLAUDE.md` of 60 lines or fewer, summarising the hard constraints (§2), the stack (§4), the conventions (§5) and the `make` commands (§6).
2. **Build strictly phase by phase (§10).** For each phase:
   1. Restate the goal and list the files you will touch, in 10 lines or fewer.
   2. **Write the phase's listed tests first.** Run them and confirm they fail for the right reason.
   3. Implement until they pass.
   4. Run `make check` (lint, format check, type check and all tests). It must be fully green.
   5. Post the phase report (§11) and commit with the message `phase N: <summary>`.
   6. **Stop and wait** for the user to say "continue".
3. **Treat the tests as the contract.** Never weaken, skip, mark `xfail` or delete a test to make it pass. If a test in this brief looks wrong, say so in the phase report and propose a change.
4. **Ask before you:**
   - add a dependency that is not listed in §4;
   - change a public interface from §7;
   - change anything in §2.
5. **Never contact a real site.** Do not send any request to bookmyshow.com, or to any other real website, from tests or from your own tool runs. All automated tests run against mocked HTTP and the local fake site built in Phase 6. Only the human runs live commands, and only in Phase 10.
6. **Stop at evasion.** If any task looks like it needs a way around bot detection, a CAPTCHA, a queue or rate limits, stop and ask. §2 forbids it.
7. **Inject every side effect.** Time comes through `Clock`, network through `SiteAdapter` or an injected `httpx` client, and storage through `Store`. Keep matcher and diff logic pure.

---

## 1. Product overview

**Problem.** For big releases, the good shows (the user's preferred cinema, IMAX, good rows) fill up minutes after booking opens. Booking also opens per cinema at unpredictable times. Shows go live on BookMyShow only when each cinema opens its listings, so "booking open" is an event per (city, venue, date), not per movie.

**User.** One person, booking for themselves and friends or family, on their own account.

**Modes.** Each target runs in one of three modes, which are also the rollout order:

| Mode | Behaviour |
|---|---|
| `notify-only` (default) | Watch, detect openings, alert the user. No browser automation. |
| `dry-run` | On detection, open the seat map, compute the best seats, and save a screenshot with them highlighted. Click no seats. |
| `assist` | Select seats and proceed to the order summary, where the seats are held and a countdown is visible. Then **stop** and alert the user with the countdown. The user completes payment manually. |

**Success metric.** The time from a cinema opening listings to the user holding good seats. The two parts that dominate it are detection latency (roughly half the poll interval) and the time to reach the seat map, which a warm session minimises.

---

## 2. Hard constraints (non-negotiable)

| ID | Constraint | How it is enforced |
|---|---|---|
| C1 | **Payment is never automated.** No code path enters a UPI ID, card data or OTP, or submits payment. The executor's last page is the order summary. It never clicks "continue to payment". | Steps are an allow-list with no payment step. Every click goes through `safe_click()`, which raises `PaymentBoundaryError` if `adapter.is_payment_page(page)` is true or if the target matches a forbidden locator. Tests T7.2 and T7.3. |
| C2 | **No credential handling.** The user logs in manually in a headed browser (`showwatch login`). The agent reuses the persistent browser profile and never reads, stores, logs or types passwords or OTPs. | The profile directory is gitignored and never logged. Test T7.12. |
| C3 | **No anti-bot evasion.** No CAPTCHA solving, stealth or "undetected" plugins, fingerprint spoofing, proxy or IP rotation, user-agent rotation, or multiple accounts. On a CAPTCHA, a virtual queue or waiting room, or a block page: move to `PAUSED`, leave the browser where it is, notify the user, and take no further action on that target. | Adapter interstitial detection. Tests T4.8, T7.5 and T7.6. |
| C4 | **Polite traffic.** At most 1 request every 20 s across all targets. A per-target minimum of 60 s, hard-coded, which config cannot lower. Single-flight, so no two requests are ever in flight. Exponential backoff on 429 and 5xx. A 403 pauses the target and notifies the user. | `ratelimit.py`. Tests T1.7 and T4.5–T4.11. |
| C5 | **Personal scale.** One account. At most 10 seats per target and at most 3 targets. No resale or bulk-purchase features. | Config validation. Tests T1.2 and T1.8. |
| C6 | **Headed browser by default** for the executor, so the user can take over instantly. Headless is allowed only in tests. | `browser.py` flag. |
| C7 | **Secrets live only in environment variables** (`.env`, gitignored; `.env.example` committed). Logs redact cookies, tokens, the profile path and token-bearing URLs. Telegram Bot API URLs contain the bot token, and `httpx` logs request URLs at INFO level, so redaction must cover that case. | Logging filter. Tests T5.5 and T7.12. |

*Why these exist.* In OWASP's Automated Threats taxonomy, a single user automating their own ticket purchase falls under **OAT-006 Expediting**. Bulk or unfair acquisition of limited inventory is **OAT-005 Scalping**, which is what the platform's anti-bot defences are built to stop. These constraints keep ShowWatch firmly in the first category and limit the risk to the user's account. The user is responsible for reviewing the platform's terms of use.

---

## 3. Architecture

```
 targets.yaml ─► Config ─► Scheduler ──► Watcher ──(Change)──► Orchestrator
                            ▲  Clock       │  SiteAdapter          │
                            │              ▼                       ▼
                          Store ◄──── snapshots             Matcher (pure)
                         (SQLite)                            rank_shows / best_block
                            ▲                                      │
                            └──── attempts + audit ◄──── Executor (Playwright, headed)
                                                                   │ stops at order summary
                          Notifier (Telegram / console) ◄──────────┘
                          LLM fallback (off the hot path): intent parsing, locator healing
```

| Component | Responsibility | Must not |
|---|---|---|
| Config | Load and validate YAML into `Settings` and `Target` models | Hold secrets |
| Scheduler | Compute the next poll time per target: base or hot interval, jitter, backoff, floor | Sleep with the real clock |
| Watcher | Call `adapter.check_availability`, diff against the last snapshot, emit `Change`s | Launch a browser in `notify-only` mode |
| Matcher | `rank_shows` and `best_block`: pure, deterministic, fast | Do any I/O |
| Executor | Drive the browser through the allow-listed steps up to the order summary | Click anything on the payment page |
| Store | SQLite (WAL): targets, snapshots, attempts, audit, notification dedup | — |
| Notifier | Send templated events; deduplicate by event id | Crash the pipeline on failure |
| Orchestrator | Wire everything together and own the state machine | Bypass the state machine |
| LLM fallback | Natural language → intent YAML (user confirms); propose a replacement locator when one breaks | Run on the hot path; choose payment actions |

### State machine

States: `IDLE`, `WATCHING`, `DETECTED`, `SELECTING`, `HELD`, `AWAITING_PAYMENT`, `BOOKED`, `RELEASED`, `EXPIRED`, `FAILED`, `PAUSED`.

| From | Event | To |
|---|---|---|
| IDLE | `start` | WATCHING |
| WATCHING | `change_detected` | DETECTED |
| WATCHING | `blocked` (403 / CAPTCHA / queue) | PAUSED |
| DETECTED | `notified` (mode = notify-only) | WATCHING |
| DETECTED | `begin_select` (mode = dry-run or assist) | SELECTING |
| SELECTING | `dry_run_done` | WATCHING |
| SELECTING | `no_matching_seats` | WATCHING (and notify "opened, nothing matches your rules") |
| SELECTING | `seats_held` | HELD |
| SELECTING | `blocked` | PAUSED |
| SELECTING | `error` (seat race exhausted, needs login, locator not found) | FAILED |
| HELD | `summary_reached` (notified with countdown) | AWAITING_PAYMENT |
| AWAITING_PAYMENT | `user_done` (`showwatch done <id>`) | BOOKED (terminal) |
| AWAITING_PAYMENT | `hold_expired` | EXPIRED |
| HELD, AWAITING_PAYMENT | `user_cancel` | RELEASED |
| EXPIRED, RELEASED | `rewatch` (automatic) | WATCHING |
| PAUSED, FAILED | `user_resume` | WATCHING |

**Invariants**

- A target has at most one non-terminal attempt at any time. Enforce this with a unique partial index.
- Every transition writes an audit row: `attempt_id`, `from`, `to`, `event`, `reason`, `at`.
- On startup, recover in-flight attempts:
  - `SELECTING` → `WATCHING`;
  - `HELD` or `AWAITING_PAYMENT` past `expires_at` → `EXPIRED` → `WATCHING`;
  - `HELD` or `AWAITING_PAYMENT` with time remaining stays as it is, and the countdown is notified again.

---

## 4. Tech stack

- **Language and tooling:** Python 3.12, with `uv` for environments and lockfile.
- **Runtime dependencies:** `playwright`, `httpx`, `pydantic>=2`, `pyyaml`, `typer`, `python-dotenv`. Phase 9 adds `anthropic`.
- **Dev dependencies:** `pytest`, `pytest-asyncio`, `respx`, `hypothesis`, `pytest-socket`, `ruff`, `mypy`, `types-PyYAML`, `starlette`, `uvicorn` (the last two only for the fake site).
- **Storage:** stdlib `sqlite3` in WAL mode, behind a `Store` class, with simple numbered migrations.
- **Scheduling:** your own asyncio loop driven by the injected `Clock`. No APScheduler, so tests run instantly.

---

## 5. Conventions

- `src/` layout with the package `showwatch`. `mypy --strict` must pass on `src/`.
- All I/O is async. `matcher/` and `watcher/diff.py` are pure, with no I/O and no clock.
- **Time only goes through `Clock`.** `datetime.now(` and `time.time(` are banned outside `clock.py`; architecture test T0.5 checks this.
- Store times in UTC. Show all user-facing times in `Asia/Kolkata`, via `zoneinfo`.
- **Logs** are JSON lines in `~/.showwatch/logs/`, passed through the redaction filter (C7).
- **Per-attempt artefacts** go in `~/.showwatch/runs/<attempt_id>/NN-<step>.png`.
- **Locators** use role and text (`get_by_role`, `get_by_text`, `get_by_test_id`), never deep CSS or XPath chains.
- **Errors are typed exceptions** (§7); no bare `except:`.

---

## 6. Repo layout and commands

```
showwatch/
├── CLAUDE.md
├── Makefile
├── pyproject.toml
├── .env.example                # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY
├── docs/SPEC.md
├── examples/targets.example.yaml
├── src/showwatch/
│   ├── cli.py                  # typer app
│   ├── config.py               # Settings, Target, validation
│   ├── models.py               # Show, SeatMap, Snapshot, Change, Attempt, Event
│   ├── clock.py                # Clock protocol, SystemClock
│   ├── logging.py              # JSON logger + redaction filter
│   ├── store.py                # SQLite store + migrations
│   ├── state.py                # State enum, transition table, StateMachine
│   ├── ratelimit.py            # global limiter, backoff, single-flight
│   ├── watcher/{diff.py, scheduler.py, watcher.py}
│   ├── matcher/{shows.py, seats.py}
│   ├── executor/{browser.py, executor.py, guards.py}
│   ├── adapters/{base.py, fake.py, bms.py}
│   ├── notify/{base.py, console.py, telegram.py}
│   ├── llm/{intent_parser.py, healer.py}        # Phase 9
│   └── orchestrator.py
├── tests/
│   ├── conftest.py             # FakeClock, tmp store, network lockdown, fake-site fixture
│   ├── unit/   ├── integration/   ├── e2e/
│   └── fakesite/               # Starlette app, templates, control API
└── scripts/calibrate.py        # Phase 10 (run by the user)
```

**Makefile targets**

| Target | What it runs |
|---|---|
| `make check` | `ruff check`, `ruff format --check`, `mypy --strict src`, then `pytest -q` (all markers) |
| `make test` | `pytest` only |
| `make e2e` | `pytest -m e2e` |
| `make fakesite` | Runs the fake site for manual poking |

**CLI commands**

`login`, `add <yaml>`, `list`, `status [id]`, `run`, `pause <id>`, `resume <id>`, `done <attempt_id>`, `cancel <attempt_id>`, `heartbeat`, `canary`, and `parse "<natural language>"` (Phase 9).

---

## 7. Key interfaces (public; ask before changing)

```python
class Clock(Protocol):
    def now(self) -> datetime: ...                    # tz-aware UTC
    async def sleep(self, seconds: float) -> None: ...

class SeatStatus(Enum):
    AVAILABLE = "A"; TAKEN = "T"; GAP = "_"           # GAP = aisle / no seat

@dataclass(frozen=True)
class SeatMap:
    rows: tuple[tuple[str, tuple[SeatStatus, ...]], ...]   # row label + seats, in on-screen order

@dataclass(frozen=True)
class SeatBlock:
    row: str
    start_col: int
    n: int

@dataclass(frozen=True)
class Show:
    show_id: str
    venue: str
    screen_format: str          # "IMAX 2D", "4DX", "2D", ...
    language: str
    starts_at: datetime
    min_price: int              # rupees
    url: str

@dataclass(frozen=True)
class Snapshot:
    target_id: str
    taken_at: datetime
    open_dates: Mapping[date, tuple[Show, ...]]       # a date absent = not open yet

class ChangeKind(Enum):
    DATE_OPENED = "date_opened"; SHOW_ADDED = "show_added"

@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    target_id: str
    on_date: date
    shows: tuple[Show, ...]

# Typed errors
class RateLimited(Exception): ...
class Blocked(Exception):                 # .reason in {"captcha", "queue", "forbidden"}
    def __init__(self, reason: str) -> None: ...
class TransientError(Exception): ...
class LocatorNotFound(Exception):         # .step names the failing step
    def __init__(self, step: str) -> None: ...
class PaymentBoundaryError(Exception): ...
class IllegalTransition(Exception): ...

class SiteAdapter(Protocol):
    async def check_availability(self, target: Target) -> Snapshot: ...   # raises RateLimited / Blocked / TransientError
    def locators(self) -> LocatorSet: ...                                 # named steps -> locator specs
    def forbidden(self) -> LocatorSet: ...                                # e.g. continue-to-payment
    async def detect_interstitial(self, page: Page) -> str | None: ...    # "captcha" | "queue" | "login" | None
    async def is_payment_page(self, page: Page) -> bool: ...
    async def read_seat_map(self, page: Page) -> SeatMap: ...
    async def read_hold_expiry(self, page: Page) -> datetime | None: ...

class Notifier(Protocol):
    async def send(self, event: Event) -> None: ...

# Pure functions
def diff(prev: Snapshot | None, curr: Snapshot) -> list[Change]: ...
def rank_shows(shows: Sequence[Show], target: Target) -> list[Show]: ...
def best_block(seat_map: SeatMap, n: int,
               preferred_rows: Sequence[str], avoid_rows: Sequence[str]) -> SeatBlock | None: ...
```

### Seat scoring (exact definition)

1. **Candidates.** Every run of `n` contiguous `AVAILABLE` seats in a row that is not in `avoid_rows`. A run may not contain `TAKEN` or `GAP`.
2. **Centre distance.** `centre = (len(row) - 1) / 2` and `mid = start + (n - 1) / 2`. The score subtracts `abs(mid - centre)`.
3. **Row bonus.** A row in `preferred_rows` gets `10 * (len(preferred_rows) - index)`. Other rows get 0.
4. **Score.** `score = row_bonus - abs(mid - centre)`. Highest wins.
5. **Ties.** Break ties by the row's order in the `SeatMap`, then by the lower `start_col`.

### Show ranking (exact definition)

1. **Filter out** shows that fail any of these:
   - `language` must match;
   - `screen_format` must be in `formats`;
   - `starts_at` (local time) must fall inside `showtime_window`, inclusive at both ends;
   - `min_price` must be ≤ `max_price_per_seat`;
   - the venue must be in `venues`. An empty `venues` list means any venue.
2. **Sort** by venue rank (any venue counts as equal), then format rank, then earliest `starts_at`, then `show_id`.

---

## 8. Configuration (`targets.yaml`)

```yaml
version: 1
timezone: Asia/Kolkata
notifier: telegram            # telegram | console
polling:
  base_interval_s: 900
  hot_interval_s: 60          # values below the 60 s floor are REJECTED
  jitter_pct: 20              # 0..30
  hot_windows:
    - { days: [Mon, Tue, Wed, Thu, Fri, Sat, Sun], start: "08:00", end: "13:00" }
heartbeat_at: "09:00"
targets:                      # max 3
  - id: movie-fri
    mode: notify-only         # notify-only | dry-run | assist
    movie: "<title>"
    city: bengaluru
    language: hindi
    formats: [IMAX 2D, 4DX, 2D]           # preference order, non-empty
    venues: ["<cinema A>", "<cinema B>"]  # ranked; [] = any venue
    dates: [2026-12-18, 2026-12-19]
    showtime_window: { start: "18:00", end: "22:30" }
    seats: 2                              # 1..10
    preferred_rows: [H, J, K]
    avoid_rows: [A, B, C]
    max_price_per_seat: 600
```

Pydantic models use `extra="forbid"`. Secrets are never accepted from YAML.

---

## 9. Testing strategy

**Test pyramid**

| Layer | Marker | What it covers | Speed target |
|---|---|---|---|
| Unit | `unit` | Pure logic | Under 1 s total |
| Integration | `integration` | Store, watcher with a stub adapter, notifier with `respx` | — |
| End-to-end | `e2e` | Playwright against the local fake site | — |

**Network lockdown.** There are two layers, because the Python process and the browser process are separate.

1. **Python process:** `pytest-socket` restricts connections to `127.0.0.1` and `::1`. Verify the exact flags against the pytest-socket docs; T0.3 proves the lockdown works.
2. **Browser process:** pytest-socket does not cover the browser. The e2e fixture installs `context.route("**/*", ...)`, which aborts any request whose host is not `127.0.0.1`; T6.8 proves it.

`respx` intercepts `httpx` calls before they reach a socket.

**`FakeClock`.** `now()` returns controlled time. `sleep(s)` advances the time instantly and records the call.

**Fake site ("FakeShow").** This is a test harness, not a copy of BookMyShow.

- **Branding:** plain, unbranded semantic HTML with ARIA roles. No BookMyShow name, logo or styling (T6.7 checks this).
- **Hosting:** runs as a uvicorn subprocess on `127.0.0.1` with a random port.
- **Normal flow pages:** movie page (coming soon / book) → venue and date listing → quantity dialog → seat map → order summary → payment page → confirmation.
  - The seat map is a grid of buttons with `data-row`, `data-col`, `aria-pressed` and gap cells.
  - The order summary shows a hold countdown and a "Continue to payment" button.
  - The payment page has a form whose "Pay now" button POSTs to `/pay`.
- **Interstitial pages:** `/captcha`, `/queue`, `/login`, and a 403 block page.
- **Control API:** `POST /__control/state` accepts these keys:

  | Key | What it sets |
  |---|---|
  | `booking_open` | Whether the movie page offers booking |
  | `shows` | The list of shows per venue and date |
  | `seat_layout` | The seat map grid, including gaps |
  | `taken` | Which seats are already taken |
  | `interstitial` | Which interstitial page to serve, if any |
  | `status_code` | Forced HTTP status for responses |
  | `latency_ms` | Artificial response delay |
  | `steal_seats_on_click` | Mark seats as taken when clicked, to simulate losing a race |
  | `hold_seconds` | Length of the summary page countdown |
  | `rename_locator` | Rename a button, to simulate a UI change |

- **Request log:** `GET /__control/log` returns every request (method, path, time) for assertions.

**Coverage.** At least 90% for `matcher/`, `watcher/diff.py` and `state.py`; at least 80% overall.

---

## 10. Build phases and gate tests

Each phase is done only when **all of its tests pass and `make check` is green**. Then you stop for review.

### Phase 0 — Skeleton and test harness basics
**Deliverables:** `pyproject.toml`, `Makefile`, `CLAUDE.md`, `docs/SPEC.md`, package skeleton, a `showwatch --help` stub, and a `conftest.py` with the network lockdown and `FakeClock`.

| ID | Test | Expected |
|---|---|---|
| T0.1 | Import the package | `showwatch.__version__` exists |
| T0.2 | `CliRunner` invokes `--help` | Exit code 0; all §6 commands are listed |
| T0.3 | Open a socket to a non-local host inside a test | Raises (the lockdown works) |
| T0.4 | `FakeClock.sleep(30)` | `now()` advances 30 s; wall time is under 0.1 s |
| T0.5 | Architecture test: scan `src/` | No `datetime.now(` or `time.time(` outside `clock.py` |

### Phase 1 — Config and domain models

| ID | Test | Expected |
|---|---|---|
| T1.1 | Load `examples/targets.example.yaml` | Valid `Settings`; times parse in IST |
| T1.2 | `seats: 0` and `seats: 11` | Both rejected with a clear message |
| T1.3 | `showtime_window` with start ≥ end | Rejected |
| T1.4 | Empty `formats`; empty `venues` | `formats` rejected; empty `venues` accepted as "any venue" |
| T1.5 | Unknown key, e.g. `seat: 2` | Rejected; the message names the key |
| T1.6 | A date before `FakeClock.now()` | Rejected |
| T1.7 | `hot_interval_s: 30` | Rejected (below the 60 s floor, C4) |
| T1.8 | Four targets | Rejected (C5) |
| T1.9 | `mode` omitted | Defaults to `notify-only` |
| T1.10 | `telegram_token` key in YAML | Rejected; the message points to `.env` |
| T1.11 | Duplicate target ids | Rejected |

### Phase 2 — Matcher (pure)

| ID | Test | Expected |
|---|---|---|
| T2.1 | Single fully free row of 20, n=2 | Block centred (cols 9–10) |
| T2.2 | Row with a GAP and TAKEN seats near the centre | The block never spans a GAP or TAKEN seat |
| T2.3 | Preferred row with an off-centre block vs. a perfectly central block in a non-preferred row | Preferred row wins |
| T2.4 | Two preferred rows with equal centrality | The earlier-listed preferred row wins |
| T2.5 | No contiguous run of n seats anywhere | Returns `None` |
| T2.6 | n=1; n equal to the row length | Correct block, or `None` if any seat in the row is unavailable |
| T2.7 | `avoid_rows` contains the only good row | That row is never chosen |
| T2.8 | Hypothesis property test | Any result is in bounds, has length n, all AVAILABLE, and the row is not avoided |
| T2.9 | Performance: 30×40 grid, n=4, 100 runs | Mean under 5 ms |
| T2.10 | Shows outside the window, including boundary times | Boundaries are inclusive; outside times are filtered |
| T2.11 | Wrong language or format not listed | Filtered |
| T2.12 | `min_price` above max | Filtered |
| T2.13 | Mixed venues, formats and times | Order is venue → format → earliest time → `show_id` |
| T2.14 | `venues: []` | All venues are eligible; ranked by format, then time |
| T2.15 | Shuffled input order | Same output (deterministic) |

### Phase 3 — Store and state machine

| ID | Test | Expected |
|---|---|---|
| T3.1 | Every legal transition in §3 | Succeeds and writes one audit row |
| T3.2 | Illegal transition, e.g. WATCHING → BOOKED | `IllegalTransition`; nothing written |
| T3.3 | Two concurrent tasks create an active attempt for the same target | Exactly one succeeds |
| T3.4 | Startup with an attempt in SELECTING | Recovered to WATCHING |
| T3.5 | Startup with AWAITING_PAYMENT, expired / not expired | Expired → EXPIRED → WATCHING; not expired → stays, and the countdown is notified again |
| T3.6 | Any transition out of BOOKED | Raises (terminal state) |
| T3.7 | Close and reopen the DB file | Data persists; `journal_mode=wal` |
| T3.8 | Run migrations twice | Idempotent; no error |

### Phase 4 — Watcher, scheduler, rate limiting
These tests use a scripted `StubAdapter`, which returns snapshots or raises typed errors, plus `FakeClock`.

| ID | Test | Expected |
|---|---|---|
| T4.1 | First snapshot ever | Recorded as the baseline; **no** change emitted |
| T4.2 | A date goes from closed to open | One `DATE_OPENED`; identical repeat snapshots emit nothing |
| T4.3 | A new show on an already-open date | One `SHOW_ADDED` containing only the new show |
| T4.4 | A show disappears | No change emitted (logged only) |
| T4.5 | Interval outside vs. inside a hot window | Base vs. hot interval; never below 60 s |
| T4.6 | Jitter over 1,000 samples | Within ±`jitter_pct`; never below the floor |
| T4.7 | `RateLimited` repeatedly | Interval doubles each time, capped at 60 min; resets after 3 consecutive successes |
| T4.8 | `Blocked("captcha")` / `Blocked("forbidden")` | Target PAUSED; exactly one notification; no polls until resume |
| T4.9 | `TransientError` five times in a row | Loop survives; backoff applies; one notification after the 5th failure |
| T4.10 | Three targets polling together | Request start times are at least 20 s apart (global limiter) |
| T4.11 | Instrumented adapter | `check_availability` calls never overlap (single-flight) |
| T4.12 | A change, then the same state on the next poll | Exactly one attempt and exactly one DETECTED event |

### Phase 5 — Notifier

| ID | Test | Expected |
|---|---|---|
| T5.1 | Telegram send, with `respx` mocking the Bot API | Correct `chat_id`, text and `parse_mode` |
| T5.2 | HELD event message | Includes seats, venue, show time in IST, hold expiry time and minutes left |
| T5.3 | Bot API returns 5xx | Up to 3 retries with backoff, then an error is logged; the pipeline continues |
| T5.4 | Same event id sent twice | Delivered once (dedup in `Store`) |
| T5.5 | Error path with `caplog` capturing all loggers, including `httpx` | The bot token never appears in any log record |
| T5.6 | Telegram notifier with env vars missing | Clear startup error; the console notifier still works |
| T5.7 | Heartbeat | Lists every target with its state and last poll time |

### Phase 6 — Fake site and `FakeSiteAdapter`

| ID | Test | Expected |
|---|---|---|
| T6.1 | Start and stop the fixture | Binds `127.0.0.1` on a random port; health check OK; clean shutdown |
| T6.2 | Control API flips `booking_open` | The movie page reflects it |
| T6.3 | `FakeSiteAdapter.check_availability` | Snapshot matches the configured shows and dates |
| T6.4 | `status_code: 429` / `403`; `interstitial: captcha` | Raises `RateLimited` / `Blocked("forbidden")` / `Blocked("captcha")` |
| T6.5 | Request log | Records method and path for every request |
| T6.6 | POST `/pay` | Counted in the log (used to prove C1) |
| T6.7 | Scan `tests/fakesite/` templates | No "bookmyshow" or "bms" (case-insensitive), no third-party logos |
| T6.8 | Browser fixture loads a page that references an external host | That request is aborted (browser-level lockdown) |

### Phase 7 — Executor (e2e against the fake site; headless allowed in tests)

| ID | Test | Expected |
|---|---|---|
| T7.1 | Happy path, `assist` mode | Reaches the order summary; selected seats equal `best_block` output; attempt is AWAITING_PAYMENT; under 10 s with `latency_ms: 0` |
| T7.2 | **C1 proof** after T7.1 | Request log has 0 POSTs to `/pay` and 0 GETs of the payment page |
| T7.3 | Call `safe_click` while on the payment page, or on a forbidden locator | `PaymentBoundaryError` |
| T7.4 | `steal_seats_on_click` for the first 2 tries | Re-reads the map and takes the next best block on try 3; with 3 losses → FAILED (`seat_race_exhausted`) + notification |
| T7.5 | CAPTCHA interstitial mid-flow | PAUSED; **no requests** after the interstitial; notification sent; browser left open |
| T7.6 | Queue interstitial | Same behaviour as T7.5 |
| T7.7 | Login page appears | FAILED (`needs_login`); notification tells the user to run `showwatch login` |
| T7.8 | Screenshots | One per step, numbered in order, in the attempt folder |
| T7.9 | `rename_locator` on the proceed button | `LocatorNotFound(step="proceed_to_summary")`; screenshot saved; attempt FAILED |
| T7.10 | `dry-run` mode | Highlighted seat-map screenshot written; no seat-select requests; back to WATCHING |
| T7.11 | Summary page countdown | Parsed into `expires_at` and stored on the attempt |
| T7.12 | Two runs with one persistent profile | A cookie from run 1 is present in run 2; the profile path appears in no log line |

### Phase 8 — Orchestrator and CLI (full e2e)

| ID | Test | Expected |
|---|---|---|
| T8.1 | `notify-only`: coming soon → flip open | Exactly one DETECTED notification; **no browser launched** |
| T8.2 | `assist`: flip open | DETECTED → … → AWAITING_PAYMENT notification with countdown; detection within one poll interval |
| T8.3 | Short `hold_seconds` | EXPIRED notification → back to WATCHING |
| T8.4 | Kill the orchestrator mid-SELECTING, then restart | Recovered to WATCHING; no duplicate attempt |
| T8.5 | Two targets at different venues | Both handled; no overlapping requests |
| T8.6 | `CliRunner` across every command in §6 | Correct exit codes and output; `add` validates YAML |
| T8.7 | `showwatch done <id>` | BOOKED; polling stops for that target |
| T8.8 | `canary` against the fake site, normal vs. renamed locator | Passes vs. fails and notifies |

### Phase 9 — LLM fallback (Anthropic client mocked; model name from config or env)

| ID | Test | Expected |
|---|---|---|
| T9.1 | `parse "2 IMAX tickets for <movie> this Friday evening in Bengaluru, rows H to K"` | A valid `Target` is shown as YAML; saved only after the user confirms |
| T9.2 | LLM returns invalid or extra fields | Rejected with the validation errors; nothing saved |
| T9.3 | Renamed proceed button | Healer receives a screenshot and accessibility snapshot and proposes a locator; accepted only if it matches **exactly one** visible element with the expected role; cached as *provisional* |
| T9.4 | Provisional locator, then the next canary | Promoted after the canary passes; discarded if it fails |
| T9.5 | Healer proposes an element on the payment page, a forbidden locator, or one whose name contains UPI/OTP/card/CVV | Rejected |
| T9.6 | LLM timeout | Attempt FAILED + notification; at most 1 heal call per step per attempt |
| T9.7 | Happy-path e2e | Healer call count is 0 (it stays off the hot path) |

*Note:* the seat-map button on the real site may itself say "Pay ₹X" while only leading to the order summary. That is why the boundary is defined by **page** (`is_payment_page`) and by the explicit `forbidden()` locators, not by matching the word "pay".

### Phase 10 — Live calibration (tools by Claude Code; execution by the user)

You write the tools and do **not** run them against the live site.

**Deliverables**

- **`scripts/calibrate.py`:**
  - opens a headed browser with the persistent profile;
  - the user clicks through the real flow manually;
  - for each click, the recorder logs the URL pattern, element role, accessible name and a suggested `get_by_role` locator;
  - it writes `calibration/bms-locators.draft.yaml`;
  - it marks the order-summary and payment pages, which the user confirms.
- **`adapters/bms.py`:** built from the confirmed YAML.
- **Availability check:** tries the HTTP tier first. If that is blocked or the page is JS-rendered, it falls back to reloading the page in the persistent browser at the same polite cadence. **No evasion to make the HTTP tier work (C3).**

| ID | Test | Expected |
|---|---|---|
| T10.1 | Calibration YAML schema | Valid file loads; a missing required step lists what is missing |
| T10.2 | **Contract test:** `BmsAdapter` built from a sample calibration YAML, with the fake site configured to the same roles and names | Passes the Phase 7 e2e suite (adapters are interchangeable) |
| T10.3 | Recorder unit test with a synthetic click event | Correct `get_by_role(role, name=...)` suggestion |

**Manual rollout checklist (the user runs these, in order)**

1. M10.1 — `notify-only` on a real upcoming movie: an alert arrives when a date opens.
2. M10.2 — `canary` on a currently showing movie: passes, read-only, reaching the seat map only.
3. M10.3 — `dry-run`: the screenshot shows sensible seats.
4. M10.4 — `assist` on a low-stakes show: reaches the order summary; the user pays manually and runs `showwatch done`.

---

## 11. Phase report template

```
## Phase N report
- Built: <modules/files>
- Tests: <IDs added> — <X passed, 0 failed>; coverage <matcher/state/diff %>, overall <%>
- make check: green (ruff, format, mypy --strict, pytest)
- Deviations from spec (and why):
- Open questions for the user:
- Next phase: <one line>
```

## 12. Out of scope

Out of scope are:

- automated payment;
- multiple accounts;
- resale features;
- CAPTCHA, queue or rate-limit bypass;
- proxies;
- mobile-app automation;
- other ticketing platforms (the `SiteAdapter` interface leaves room later);
- a web dashboard (a possible later FastAPI add-on).
