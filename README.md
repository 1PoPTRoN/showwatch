# ShowWatch

A personal assistant for booking movie tickets on BookMyShow. It waits for booking to open for
a movie you choose, picks the best show and seats according to your rules, and holds those seats
in your own logged-in browser. **You always make the payment yourself.**

> **Status: early development (Phase 8 of 10 done).** The whole tool works end to end, from the
> command line, against a local fake cinema site (FakeShow). It doesn't talk to BookMyShow yet: the
> real-site adapter is Phase 10, and until then `run`, `login` and `canary` need `--fakeshow URL`.

## Why

For big releases, the good shows (your preferred cinema, IMAX, good rows) sell out within minutes of
booking opening. Each cinema opens its listings at its own unpredictable time. ShowWatch keeps watch
so you don't have to keep refreshing, and gets you to the payment step as fast as possible.

## How it will work

Each watched movie (a *target*) runs in one of three modes. They are also the order in which to try them:

| Mode | What happens when booking opens |
|---|---|
| `notify-only` (default) | You get an alert. No browser automation at all. |
| `dry-run` | It opens the seat map, works out the best seats and saves a screenshot with them highlighted. It clicks nothing. |
| `assist` | It selects the seats and stops at the order summary, where they are held, then alerts you with the countdown. You pay yourself. |

## Using it

```sh
cp examples/targets.example.yaml targets.yaml   # edit it: movie, dates, cinemas, seats, ...
showwatch add targets.yaml        # validates the file, then saves its targets and settings
showwatch login                   # once: log in yourself in the browser window that opens
showwatch run                     # watch until Ctrl+C (keep it running until you have paid)
```

| Command | What it does |
|---|---|
| `add <yaml>` | Validate a targets file and add its targets (at most 3 unfinished) |
| `list`, `status [id]` | Targets, their state, when they were last checked, any held seats |
| `run` | Watch, alert, and (in `assist` mode) hold seats |
| `pause <id>`, `resume <id>` | Stop or restart watching one target |
| `done <attempt>` | You paid: mark it booked (it stops being watched) |
| `cancel <attempt>` | Stop tracking a hold (nothing is clicked; the hold lapses) and watch again |
| `remove <id>` | Delete a target and its history |
| `heartbeat` | Send the status summary now |
| `canary` | Read-only check that the booking pages still look as expected |
| `login` | Open the browser so you can log in yourself |

Data lives in `~/.showwatch` (or `$SHOWWATCH_HOME`). Only one `run`, `login` or `canary` can run at
a time. If ShowWatch is stopped mid-way, the next start recovers: an interrupted selection goes back
to watching, and a hold that is still running gets its countdown re-sent.

## The targets file

You describe what you want in a small YAML file: city, language, formats in order of preference
(e.g. IMAX 2D, then 4DX, then 2D), cinemas, dates, a showtime window, the number of seats, preferred
and avoided rows, and a maximum price. Start from
[examples/targets.example.yaml](examples/targets.example.yaml), which explains every field.

The file is checked strictly, and every problem is reported at once with the key and target it
belongs to:

```text
Invalid config (targets.yaml):
  - targets[0] (movie-fri).showtime_window.start: unquoted time: YAML reads it as the number 1080; quote it: "18:00"
  - targets[0] (movie-fri).seat: unknown key (did you mean 'seats'?)
  - targets[0] (movie-fri).dates: 2026-11-01 is in the past (today is 2026-11-03 in Asia/Kolkata)
```

- **Quote your times** (`"18:00"`). Unquoted, YAML silently turns `18:00` into the number 1080.
- **Typos are errors**, not silently ignored keys, and duplicate keys are refused.
- **Secrets are refused.** A key such as `telegram_token` is rejected with a pointer to `.env`, and
  error messages never repeat the values you wrote.
- **Limits are fixed:** 1–10 seats, at most 3 targets, polling no faster than every 60 seconds.

## How shows and seats are chosen

**Shows.** A show qualifies only if it matches your language and one of your formats, starts inside
your showtime window (local time, both ends included), costs no more than your maximum, and is at one
of your cinemas (or you listed none). Qualifying shows are ordered by your cinema ranking, then your
format ranking, then the earliest start.

**Seats.** Every run of adjacent free seats of the size you asked for is a candidate. Runs never cross
an aisle or a taken seat. A candidate scores **row bonus minus distance from the centre of the row**.
Your first preferred row is worth `10 × (number of preferred rows)`, the next one 10 less, and so on.
Other rows get no bonus, and avoided rows are never used. A preferred row therefore beats an ordinary
row unless its best free seats are further off-centre than its bonus. For your last preferred row,
that margin is 10 seats. Ties go to the row listed first in the seat map, then to the left.

## How watching works

- **Only watching targets are polled**, one request at a time, and never more than one request
  every 20 seconds across all targets.
- **Each target is polled every `base_interval_s`**, or every `hot_interval_s` (60 seconds at the
  fastest) inside a hot window. Every interval is randomised by up to `jitter_pct`. If a hot window
  is about to start, ShowWatch wakes at its start.
- **Only shows you would accept count.** A new show at a cinema you didn't list, or outside your
  showtime window, is not an opening. A show that disappears for a moment and comes back is not
  news either, because ShowWatch remembers everything it has seen.
- **If the site pushes back, ShowWatch slows down.** A "too many requests" answer doubles the wait
  for every target, up to an hour, until three polls in a row succeed. Repeated errors slow down
  just that target, and you hear about it once, at the fifth failure in a row.
- **A CAPTCHA, a queue or a block page pauses the target** and tells you. ShowWatch never tries to
  get around it.
- **If booking is already open when you add a target**, the first check says so, rather than
  waiting silently for an opening that has already happened.

## Notifications

Alerts go to **Telegram** (`notifier: telegram`) or to the **console** (`notifier: console`). For
Telegram, create a bot with @BotFather and put its token and your chat id in `.env`:

```sh
cp .env.example .env    # then fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

You are told when booking opens, when seats are held (with the venue, show time, seats and the
time left to pay), when a hold runs out, when a target is paused or fails, and once a day that
ShowWatch is still running. Each alert is delivered once. If Telegram is unavailable, ShowWatch
retries, and a failed alert is tried again later rather than lost. All times are shown in IST.

Logs are JSON lines in `~/.showwatch/logs/`. Tokens, cookies and token-bearing URLs are redacted
from every log line, including those from the HTTP library.

## What happens to a target

Each target moves through a small set of states, and every move is recorded in an audit trail:

```text
IDLE → WATCHING → DETECTED ─┬─ notify-only: alert sent → back to WATCHING
                            └─ dry-run / assist → SELECTING ─┬─ dry-run: screenshot → WATCHING
                                                             └─ assist → HELD → AWAITING_PAYMENT
                                                                    you pay → BOOKED
                                                                    hold runs out → EXPIRED → WATCHING
CAPTCHA, queue or block page → PAUSED (until you resume)      errors → FAILED (until you resume)
```

The mode is enforced by the state machine itself: a `notify-only` target can never reach seat
selection, and a `dry-run` target can never hold seats. State lives in a local SQLite database.
Every change happens completely or not at all, and if ShowWatch is stopped mid-way, it recovers on
the next start. An interrupted selection goes back to watching, an expired hold is closed, and a hold
that is still running keeps its countdown.

## How seats are held (assist mode)

When booking opens, ShowWatch opens your saved browser profile, goes to the best show, chooses the
number of seats, reads the seat map, clicks the best block of seats and proceeds to the **order
summary**. There it stops, sends you the seats, venue, show time and countdown, and leaves the
window open for you to pay.

- **It cannot pay.** The steps it may take are a fixed list with no payment step. Every click is
  checked first, and refused on the payment page or on buttons such as "Continue to payment".
  A test ensures nothing else in the code can click, and nothing at all can type.
- **If someone takes a seat first**, it reads the seat map again and tries the next best seats, up to
  three times.
- **A CAPTCHA, queue or block page stops everything at once.** The window is left exactly as it was
  and you are told. If the site asks you to log in, it tells you to run `showwatch login`.
- **Every step is screenshotted** to `~/.showwatch/runs/<attempt>/`. In `dry-run` mode it outlines the
  seats it would pick in a screenshot and clicks nothing.
- **Polling pauses** while seats are being chosen, so only one thing talks to the site at a time.

## Ground rules (built in, not optional)

- **Payment is never automated.** ShowWatch stops at the order summary. Every click passes a guard
  that refuses to act on the payment page.
- **No credentials.** You log in yourself in a normal browser window. ShowWatch reuses that browser
  profile and never sees, stores or types your password or OTP.
- **No bot-detection evasion.** No CAPTCHA solving, stealth plugins, proxies, spoofing or extra
  accounts. On a CAPTCHA, a queue or a block page it pauses, leaves the browser as it is, and tells you.
- **Polite traffic.** At most one check every 20 seconds across all targets, at least 60 seconds
  apart for any one target, and it backs off automatically when the site pushes back.
- **Personal scale.** One account, at most 3 targets, and at most 10 seats per target. No resale features.
- **Secrets stay in environment variables** (`.env`, never committed), and logs redact tokens and cookies.

ShowWatch is a personal project and is not affiliated with BookMyShow. You are responsible for
checking that your use complies with the platform's terms of use.

## Build progress

ShowWatch is built in phases. Each phase starts with its tests and lands on `main` only when every
check passes.

| Phase | Scope | Status |
|---|---|---|
| 0 | Project skeleton, CLI outline, network-locked test harness, virtual clock | Done |
| 1 | Config file and domain models | Done |
| 2 | Show ranking and seat selection | Done |
| 3 | Storage (SQLite) and the booking state machine | Done |
| 4 | Watcher, scheduler and rate limiting | Done |
| 5 | Notifications (Telegram, console) | Done |
| 6 | Local fake cinema site for testing | Done |
| 7 | Browser executor (stops at the order summary) | Done |
| 8 | Orchestrator and the full CLI | Done |
| 9 | Optional LLM helpers (plain-language targets, locator repair) | Planned |
| 10 | Calibration tools for the live site (run by you) | Planned |

## Development

You need [uv](https://docs.astral.sh/uv/), which fetches Python 3.12 for you, and `make`.

```sh
git clone https://github.com/1PoPTRoN/showwatch.git
cd showwatch
make sync                                  # create .venv from uv.lock
uv run --locked playwright install chromium   # once: the browser for the end-to-end tests
make check                                 # lint, format check, type checks and the full test suite
uv run showwatch --help
```

| Command | What it does |
|---|---|
| `make check` | ruff lint, ruff format check, `mypy --strict`, then all tests. Must be green. |
| `make test` | Tests only |
| `make e2e` | Browser end-to-end tests only |
| `make fakesite` | Run FakeShow, the local fake cinema site, to explore in your browser |
| `make fmt` | Auto-format and apply safe lint fixes |

**Tests never touch the internet.** During tests the Python process can only connect to
`127.0.0.1`/`::1`, DNS lookups of real hostnames are blocked, and no test can switch this off.
Browser tests run against **FakeShow**, a plain, unbranded fake cinema site that runs locally on
`127.0.0.1`. The test browser aborts every request to any other host.

To try the whole flow locally, start FakeShow with `make fakesite`, then pass its URL:
`showwatch run --fakeshow http://127.0.0.1:<port>`. `parse "<plain language>"` arrives in Phase 9.

## Project layout

```
src/showwatch/
  config.py         targets.yaml loading and validation
  models.py         Show, SeatMap, Snapshot, Change and friends
  matcher/          pure show ranking and seat choice (no I/O, no clock)
  state.py          the booking state machine, with mode guards
  store.py          SQLite store: targets, attempts, audit trail, poll results, crash recovery
  watcher/          change detection (pure), poll intervals, the poll loop
  ratelimit.py      the global request gate (20 s spacing, one at a time) and backoff
  notify/           message templates, Telegram and console notifiers, dedup, heartbeat
  logging.py        JSON-lines logs with secret redaction
  adapters/         site interfaces; fake.py talks to FakeShow (the real-site adapter is Phase 10)
  executor/         the browser run: guards.py (payment boundary), browser.py (saved profile)
  orchestrator.py   startup recovery, the poll loop, booking runs, hold expiry, heartbeat
  cli.py, canary.py, lock.py
  limits.py         the fixed safety limits (seats, targets, polling floor)
  clock.py, cli.py, errors.py
examples/           targets.example.yaml, the annotated starting point
tests/unit/         pure-logic tests        tests/integration/  SQLite, HTTP and fake-site tests
tests/e2e/          browser tests           tests/fakesite/     FakeShow, the local fake cinema site
tests/support/      test helpers (FakeClock, network lockdown, seat maps, targets)
docs/SPEC.md        the full build specification (source of truth)
docs/DESIGN-NOTES.md  risks, open questions and proposed resolutions
```

## License

[MIT](LICENSE)
