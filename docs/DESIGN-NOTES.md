# ShowWatch — design notes and risk register

These notes come from a full read of `docs/SPEC.md`. Each item names a gap, an ambiguity or a
stability risk, together with a **proposed** resolution. A proposal is not a decision until the user
confirms it. Items marked **[ask]** need the user's call before the phase that depends on them.
The spec stays the source of truth; nothing here relaxes §2.

Status key: `proposed` · `decided` (confirmed by the user) · `done` (implemented and tested).

## Cross-cutting

- **X1. Test time bombs.** The example dates (2026-12-18/19) are only six weeks away. Any test that validates
  dates against the real clock will start failing after 2026-12-19. → Config validation receives
  `now` from the injected `Clock` (pydantic validation context). Tests pin `FakeClock` to
  2026-11-03T03:30Z (09:00 IST). `proposed`
- **X2. Virtual time with several tasks.** "`sleep` advances time" is exact only when there is one sleeper.
  If two tasks sleep, the times add up instead of overlapping. → A single scheduler task owns all polling.
  That makes single-flight (T4.11) true by construction, and a global gate gives the 20 s spacing (T4.10).
  If real concurrency is needed later, FakeClock becomes a wake-the-earliest-sleeper scheduler. `proposed`
- **X3. Two processes.** `run` plus `canary`, or `run` plus `login`, would break C4 (each process has its own
  limiter) and fight over Chromium's profile lock. → A single-instance lock file for any command that
  touches the site or the profile. The last request time is persisted in `Store`, so the 20 s gate holds
  across processes. `proposed`
- **X4. Tooling gaps.** §9/§11 ask for coverage numbers, but no coverage tool is in §4. Hanging async
  or e2e tests are best caught by a per-test timeout. → Add `pytest-cov` and `pytest-timeout` (dev only). **[ask]**
- **X5. Warnings are errors** in pytest, so "coroutine was never awaited" and similar async bugs fail
  loudly instead of passing silently. `done` (Phase 0)
- **X6. Lockdown false passes.** A "connect is blocked" test that accepts any exception would pass
  even with no lockdown (a timeout is also an exception). → T0.3 asserts pytest-socket's specific error,
  checks that the lockdown is configured *before* it tries to connect, and only targets
  RFC 5737/3849 documentation addresses. If it ever leaked, nothing real would be reached. `done` (Phase 0)

## Phase 1 — config

- **P1.1** Apply the 60 s floor to `base_interval_s` too, not only to `hot_interval_s`, and require
  hot ≤ base. `proposed`
- **P1.2** Hot windows that cross midnight (start ≥ end): reject in v1, the same as `showtime_window` (T1.3). `proposed`
- **P1.3** "Date before now" is judged on the calendar date in the config timezone. Today is allowed. `proposed`
- **P1.4** Reject any secret-looking key (`*token*`, `*secret*`, `*password*`, `*api_key*`), not just
  `telegram_token`, with a message pointing to `.env`. `proposed`
- **P1.5** Compare language and format case-insensitively and whitespace-normalised, so "hindi" matches
  "Hindi" and "IMAX  2D" matches "IMAX 2D". Upper-case row labels. `proposed`
- **P1.6** A row in both `preferred_rows` and `avoid_rows` is contradictory → reject. `proposed`

## Phase 2 — matcher

- **P2.1 T2.3 versus the exact formula.** The last preferred row's bonus is only 10. A preferred block more
  than 10 cells off-centre therefore *loses* to a central block in a non-preferred row, which happens on
  wide rows (40 seats). The formula is the contract, so T2.3 will use an offset under 10, plus one extra
  test that documents the crossover. Change the formula only if the user wants "preferred always wins". **[ask]**
- **P2.2** `start_col` indexes on-screen cells, gaps included. It is not a printed seat number. `proposed`
- **P2.3** The show window compares local `Asia/Kolkata` time, with both ends inclusive. `proposed`

## Phase 3 — store and state machine

- **P3.1 Target state versus attempt state.** §3 mixes target-level states (IDLE, WATCHING, PAUSED) with
  attempt-level ones. → The target row holds the current state. An attempt is created on
  `change_detected` and closed when the target returns to WATCHING or reaches BOOKED. An attempt is
  *active* while it is in DETECTED/SELECTING/HELD/AWAITING_PAYMENT/FAILED, and a unique partial index on
  `attempts(target_id) WHERE active` enforces one per target. Audit rows from target-only transitions
  (start, blocked while WATCHING, resume) have `attempt_id = NULL`. `proposed`
- **P3.2 Recovery needs transitions §3 doesn't list.** SELECTING → WATCHING and HELD → EXPIRED are not in
  the table, and DETECTED (a crash before notify) is not covered at all. → Add system events
  `recover` (DETECTED|SELECTING → WATCHING) and allow `hold_expired` from HELD too. **[ask]** (it edits §3)
- **P3.3** stdlib `sqlite3` is synchronous. → Use one connection, run calls via `asyncio.to_thread`
  under an `asyncio.Lock`, and use `BEGIN IMMEDIATE` for writes. `proposed`
- **P3.4 Dedup versus re-notify.** Event ids are deterministic (`<attempt>:<kind>`), so the countdown
  re-notification on recovery needs its own id (for example `...:recovered:<boot>`). Otherwise dedup
  swallows it. `proposed`

## Phase 4 — watcher, scheduler, rate limiting

- **P4.1** Diff on `show_id`, not on dataclass equality. Otherwise a price change looks like a new show. `proposed`
- **P4.2 Flapping listings.** If a show or date vanishes for one poll (a partial response) and then
  returns, diffing against only the last snapshot fires a second DATE_OPENED/SHOW_ADDED. That means a
  duplicate alert or a second hold. → The watcher passes a "seen" baseline (the union of everything
  seen) as `prev`. `diff()` stays pure and its signature is unchanged. `proposed`
- **P4.3** If the next base-interval poll would land after a hot window starts, wake at the window
  start instead. This saves up to 15 min of latency at 08:00. `proposed`
- **P4.4** Jitter near the floor: with hot = 60 and ±20 %, clamping puts about half the samples at
  exactly 60. That satisfies T4.6. `proposed`
- **P4.5 Backoff scope.** A 429 means "this client is too fast", so back off *globally*. 5xx and
  transient errors back off per target. **[ask]**
- **P4.6 C4 versus the executor.** 1 request per 20 s cannot cover a click-through that is a dozen
  page loads in a few seconds. → C4's numbers govern availability polls. While an executor session
  runs, all polling is suspended, so only one site interaction happens at a time. **[ask]** (C4 wording)
- **P4.7** Rewatch after EXPIRED or RELEASED must never re-trigger on shows already seen. Otherwise
  ShowWatch re-holds seats in a loop, which looks like inventory hoarding. The baseline in P4.2 gives
  this for free; add a test. `proposed`
- **P4.8** T4.12 asserts "one attempt", so Phase 4 needs a thin hook into Store and StateMachine.
  The full orchestrator stays in Phase 8. `proposed`

## Phase 5 — notifier and logging (C7)

- **P5.1** A `logging.Filter` on a *logger* never sees records from child loggers such as `httpx`, and
  `caplog` uses its own handler. → Redact when the record is created (a LogRecord factory), keep
  handler filters as a second layer, and set the `httpx`/`httpcore` loggers to WARNING. `proposed`
- **P5.2** `httpx` exception text includes the URL, and so the token. → Never log raw exception text
  or tracebacks from the Telegram client; log a sanitised summary instead. `proposed`
- **P5.3** Use `parse_mode=HTML` with escaping. MarkdownV2 makes every `-`, `.`, `(` in a movie title a
  potential 400 error. `proposed`

## Phases 6–7 — fake site and executor

- **P6.1** Playwright needs a one-off Chromium download (about 150 MB) from Playwright's CDN. I will ask
  before running it. **[ask]** (at Phase 6)
- **P6.2** An optional third lockdown layer in the test fixture: Chromium `--host-resolver-rules`, so
  that no hostname except loopback resolves. `proposed`
- **P6.3** A Chromium profile is single-process. The executor keeps one warm persistent context, and
  `login` refuses to run while `run` holds the lock (see X3). `proposed`
- **P7.1** T7.5 "no requests after the interstitial" is asserted from the server request log. Fake
  interstitial pages therefore load no sub-resources and run no scripts. `proposed`
- **P7.2** If the process crashes while HELD, the browser closes, but the hold may survive on the
  site. Recovery re-notifies the countdown; ShowWatch does not try to restore the page. `proposed`
- **P7.3** `cancel` stops tracking only. ShowWatch clicks nothing on the site, and the hold lapses by
  itself. `proposed`

## Phases 8–10

- **P8.1 Which opening to act on.** Venues open at different times. With "first eligible opening wins", a
  lower-ranked venue can be held before the user's top venue opens (one active attempt per target).
  The alternative is to auto-hold only the top venue and just notify for the others. **[ask]** (by Phase 8)
- **P8.2** When every date of a target is in the past, stop it automatically and notify. `proposed`
- **P10.1** Calibration drafts may contain account-specific URLs, so `calibration/*.draft.yaml` is
  gitignored. `done` (Phase 0)
