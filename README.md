# ShowWatch

A personal assistant for booking movie tickets on BookMyShow. It waits for booking to open for
a movie you choose, picks the best show and seats according to your rules, and holds those seats
in your own logged-in browser. **You always make the payment yourself.**

> **Status: early development (Phase 1 of 10 done).** The config file format and its validation are
> in place. Nothing talks to any website yet, and the CLI commands are placeholders.

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
| 2 | Show ranking and seat selection | Planned |
| 3 | Storage (SQLite) and the booking state machine | Planned |
| 4 | Watcher, scheduler and rate limiting | Planned |
| 5 | Notifications (Telegram, console) | Planned |
| 6 | Local fake cinema site for testing | Planned |
| 7 | Browser executor (stops at the order summary) | Planned |
| 8 | Orchestrator and the full CLI | Planned |
| 9 | Optional LLM helpers (plain-language targets, locator repair) | Planned |
| 10 | Calibration tools for the live site (run by you) | Planned |

## Development

You need [uv](https://docs.astral.sh/uv/), which fetches Python 3.12 for you, and `make`.

```sh
git clone https://github.com/1PoPTRoN/showwatch.git
cd showwatch
make sync               # create .venv from uv.lock
make check              # lint, format check, type checks and the full test suite
uv run showwatch --help
```

| Command | What it does |
|---|---|
| `make check` | ruff lint, ruff format check, `mypy --strict`, then all tests. Must be green. |
| `make test` | Tests only |
| `make e2e` | Browser end-to-end tests only (from Phase 6) |
| `make fakesite` | Run the local fake site to explore by hand (from Phase 6) |
| `make fmt` | Auto-format and apply safe lint fixes |

**Tests never touch the internet.** During tests the Python process can only connect to
`127.0.0.1`/`::1`, DNS lookups of real hostnames are blocked, and no test can switch this off.
Browser tests (from Phase 6) run against a local fake site.

### CLI

`login`, `add <yaml>`, `list`, `status [id]`, `run`, `pause <id>`, `resume <id>`, `done <attempt_id>`,
`cancel <attempt_id>`, `heartbeat`, `canary`, and `parse "<plain language>"`. For now they are all
placeholders: each one prints the phase that implements it and exits.

## Project layout

```
src/showwatch/
  config.py         targets.yaml loading and validation
  models.py         Show, SeatMap, Snapshot, Change and friends
  limits.py         the fixed safety limits (seats, targets, polling floor)
  clock.py, cli.py, errors.py
examples/           targets.example.yaml, the annotated starting point
tests/unit/         pure-logic tests        tests/support/  test helpers (FakeClock, network lockdown)
docs/SPEC.md        the full build specification (source of truth)
docs/DESIGN-NOTES.md  risks, open questions and proposed resolutions
```

## License

[MIT](LICENSE)
