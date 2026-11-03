# ShowWatch — design notes and risk register

These notes come from a full read of `docs/SPEC.md`. Each item names a gap, an ambiguity or a
stability risk, together with a **proposed** resolution. A proposal is not a decision until the user
confirms it. Items marked **[ask]** need the user's call before the phase that depends on them.
The spec stays the source of truth; nothing here relaxes §2.

Status key: `proposed` · `decided` (confirmed by the user) · `done` (implemented and tested).

## Cross-cutting

- **X1. Test time bombs.** The example dates (2026-12-18/19) are only six weeks away. Any test that validates
  dates against the real clock will start failing after 2026-12-19. → `parse_settings` takes the
  injected `Clock`. Tests pin `FakeClock` to 2026-11-03T03:30Z (09:00 IST). `done` (Phase 1)
- **X2. Virtual time with several tasks.** "`sleep` advances time" is exact only when there is one sleeper.
  If two tasks sleep, the times add up instead of overlapping. → A single scheduler task owns all polling.
  That makes single-flight (T4.11) true by construction, and a global gate gives the 20 s spacing (T4.10).
  If real concurrency is needed later, FakeClock becomes a wake-the-earliest-sleeper scheduler. `done`
  (Phase 4: one WatchLoop task; the gate's lock is held while it sleeps, so only one task ever sleeps.)
- **X3. Two processes.** `run` plus `canary`, or `run` plus `login`, would break C4 (each process has its own
  limiter) and fight over Chromium's profile lock. → A single-instance lock file for any command that
  touches the site or the profile. The last request time is persisted in `Store`, so the 20 s gate holds
  across processes. `proposed`
- **X4. Tooling gaps.** §9/§11 ask for coverage numbers, but no coverage tool is in §4. Hanging async
  or e2e tests are best caught by a per-test timeout. → Add `pytest-cov` and `pytest-timeout` (dev only). **[ask]**
  Meanwhile, coverage is measured with a throwaway tool that changes no project files:
  `uv run --locked --with coverage coverage run --branch --source=src/showwatch -m pytest`.
- **X5. Warnings are errors** in pytest, so "coroutine was never awaited" and similar async bugs fail
  loudly instead of passing silently. `done` (Phase 0)
- **X6. Lockdown false passes.** A "connect is blocked" test that accepts any exception would pass
  even with no lockdown (a timeout is also an exception). → T0.3 asserts pytest-socket's specific error,
  checks that the lockdown is configured *before* it tries to connect, and only targets
  RFC 5737/3849 documentation addresses. If it ever leaked, nothing real would be reached. `done` (Phase 0)

## Phase 1 — config

- **P1.1** Apply the 60 s floor to `base_interval_s` too, not only to `hot_interval_s`, and require
  hot ≤ base. `done`
- **P1.2** Hot windows that cross midnight (start ≥ end): reject in v1, the same as `showtime_window` (T1.3). `done`
- **P1.3** "Date before now" is judged on the calendar date in the config timezone. Today is allowed. `done`
- **P1.4** Reject any secret-looking key (`*token*`, `*secret*`, `*password*`, `*api_key*`, `*chat_id*`, and
  so on), not just `telegram_token`, with a message naming the `.env` variable. Error messages never
  echo input values, and the pydantic error chain (whose text includes inputs) is suppressed. `done`
- **P1.5** `normalize_label()` compares language, format and venue ignoring case and spacing. In Phase 1
  it is used for duplicate detection; Phase 2's matcher uses it for matching. Row labels are
  upper-cased. `done` (config side)
- **P1.6** A row in both `preferred_rows` and `avoid_rows` is contradictory → reject. `done`
- **P1.7 YAML and pydantic traps that would corrupt values silently** (found by probing, all now
  refused with a hint): an unquoted `18:00` is the integer 1080, which pydantic turns into
  **00:18 UTC**; duplicate keys keep the last value; `yes`/`true` count as `1` for plain ints and
  for `Literal[1]`; `ON` becomes `True`; an integer date becomes a Unix timestamp. Numeric titles
  (`movie: 1917`) are kept as text, but `007` reaches us as 7, so the example file says to quote such
  titles. `done`
- **P1.8 The past-date rule is for user input only.** A target added on Nov 3 for Nov 5 is reloaded
  from the store after a restart on Nov 6. A model-level date check would then crash on startup.
  `Target` itself therefore accepts past dates, and `parse_settings` checks them on the way in
  (see P8.2 for runtime expiry). `done`
- **P1.9** The C4/C5 numbers live only in `limits.py`, pinned by `test_limits.py`, so config validation
  and the Phase 4 limiter cannot drift apart. `done`
- **P1.10** `showwatch add` must enforce "at most 3 targets" across the whole store, not only within
  one file. `done` (Phase 3, see P3.9)

## Phase 2 — matcher

- **P2.1 T2.3 versus the exact formula.** The last preferred row's bonus is only 10. A preferred block more
  than 10 cells off-centre therefore *loses* to a central block in a non-preferred row, which happens on
  wide rows (40 seats). The formula is the contract, so T2.3 uses an offset under 10, and
  `test_t2_3_crossover_beyond_the_bonus_is_documented` pins the crossover. `decided` (the user said "move
  ahead" on the recommendation to keep §7 as written)
- **P2.2** `start_col` indexes on-screen cells, gaps included. It is not a printed seat number. `done`
- **P2.3** The show window compares local wall-clock time, with both ends inclusive. `done`
- **P2.4 `rank_shows` needs a timezone.** The §7 signature `rank_shows(shows, target)` cannot know which
  wall clock the window is written in. → An optional keyword `tz=` defaults to Asia/Kolkata, so calls
  written to the §7 signature still work. The orchestrator passes `settings.tz`. `done` (flagged in the
  Phase 2 report as an additive interface change)
- **P2.5 Exact arithmetic.** Seat scores are computed doubled, as integers, so float rounding can
  never decide a tie. `done`
- **P2.6 Total order.** Fields after `show_id` break any leftover tie, so the result never depends on
  input order, even if a site repeats an id. `done`
- **P2.7 Venue names must match the site's names** (ignoring case and spacing). "PVR Orion" will not
  match "PVR: Orion Mall, Rajajinagar". → Revisit in Phase 10: calibration should list the exact
  venue names so they can be copied into targets.yaml, or matching gets an explicit alias list. Never
  fuzzy-match silently. `proposed`
- **P2.8** Row labels match case-insensitively. An avoided row is never chosen, even when it is also
  preferred (config forbids that overlap anyway). `done`

## Phase 3 — store and state machine

- **P3.1 Target state versus attempt state.** §3 mixes target-level states (IDLE, WATCHING, PAUSED) with
  attempt-level ones. → The target row holds the current state (the single source of truth). An attempt
  is created on `change_detected` and closed when the target returns to WATCHING or reaches BOOKED. One
  rule, no exceptions: FAILED, EXPIRED, RELEASED and PAUSED-mid-selection keep it open until then. A
  unique partial index `attempts(target_id) WHERE closed_at IS NULL` enforces one open attempt per
  target. Audit rows from target-only transitions have `attempt_id = NULL`. `decided` (user: "continue")
- **P3.2 Recovery transitions.** Added `recover` (DETECTED|SELECTING → WATCHING), and `hold_expired` is
  allowed from HELD too. `decided` (user: "continue")
- **P3.3** stdlib `sqlite3` is synchronous. → One connection per Store, with calls in `asyncio.to_thread`
  under a **thread** lock. An asyncio lock would be released when a task is cancelled while its thread
  still runs, which would put two threads on one connection. Writes use `BEGIN IMMEDIATE`, re-reading
  state under the write lock. `done`
- **P3.4 Dedup versus re-notify.** Event ids are deterministic (`<attempt>:<kind>`), so the countdown
  re-notification on recovery needs its own id (for example `...:recovered:<boot>`). Otherwise dedup
  swallows it. `proposed` (Phase 5)
- **P3.5 `user_pause`.** §6 has `showwatch pause`, but §3 has no transition for it. → Added
  `user_pause` (IDLE|WATCHING → PAUSED). `done` (flagged in the Phase 3 report)
- **P3.6 Mode guards live in the state machine.** notify-only can never reach SELECTING (no browser,
  T8.1), and dry-run can never reach HELD (no seats ever clicked). Both are proven by searching every
  state each mode can reach, not just by spot checks. `done`
- **P3.7 A crash between detection and the alert must not lose the alert.** Recovery reports
  attempts it closed with `recover`, together with their stored `Change`, so the orchestrator can
  re-send the opening alert (deduped, P3.4). A HELD attempt whose expiry was never read is treated as
  expired: it cannot be tracked after a restart. `done`
- **P3.8 Atomicity is tested, not assumed.** A test makes the last write of a transition fail (via a
  real SQLite trigger) and checks that the earlier writes were rolled back. This found that no earlier
  test proved it. Tables are STRICT, which caught a bytes-into-TEXT bug on the first run. `done`
- **P3.9** At most 3 *unfinished* (non-BOOKED) targets, enforced in `Store.add_target` (closes P1.10).
  §6 has no `remove` command, so a target that is never booked would occupy a slot for good.
  → Phase 8 needs a way to retire targets (for example `showwatch remove <id>`). **[ask]** (by Phase 8)

## Phase 4 — watcher, scheduler, rate limiting

- **P4.1** Diff on `show_id`, not on dataclass equality, so a price change is not a new show. `done`
- **P4.2 Flapping listings.** The watcher diffs against `merge_seen()`, the union of everything seen,
  not against the last poll alone. `diff()` stays pure and its signature is unchanged. `done`
- **P4.3** If the next base-interval poll would land after a hot window starts, wake at the start
  instead (no jitter on that wake, and none while backing off). `done`
- **P4.4** Jitter near the floor clamps at 60 s (T4.6). `done`
- **P4.5 Backoff scope.** A 429 doubles a *global* multiplier, and every scheduled target is pushed
  back. Other failures back off only the failing target. A target uses the larger of the two
  multipliers; each resets after 3 consecutive successes. `decided` (user: "continue")
- **P4.6 C4 versus the executor.** C4's numbers govern availability polls. `PollGate.exclusive()`
  lets a booking run hold the gate, so polling waits meanwhile. `decided` (user: "continue")
- **P4.7** Rewatch never re-triggers on shows already seen: guaranteed by the P4.2 baseline. `done`
- **P4.8 Detection is atomic with the poll.** `Store.record_poll()` saves the snapshot, moves the
  baseline forward and opens the attempt in one transaction. A crash can neither lose a detection
  nor duplicate one. `done`
- **P4.9 "Already open" notice.** T4.1 makes the first snapshot a baseline, so a target added after
  booking opened would wait in silence forever. → The first poll sends a one-off `ALREADY_OPEN`
  event. It is not a detection: no attempt and no state change. `done` (flagged in the Phase 4 report)
- **P4.10 One poll can open several dates** (a whole weekend). → An attempt carries *every* change
  from its poll (`Attempt.changes`). Migration 2 converts stored single-change rows instead of
  editing migration 1. `done`
- **P4.11 Only relevant changes count.** The watcher diffs `relevant_view()`: the target's dates
  and shows `rank_shows` accepts. A new show at a cinema you didn't list is logged, not alerted. `done`
- **P4.12 Only WATCHING targets are polled.** During DETECTED/SELECTING/HELD/AWAITING_PAYMENT the
  target is being acted on, so it isn't polled. Shows that open meanwhile are found on the first
  poll after it returns to WATCHING, because they are not in the baseline. `done`
- **P4.13 A target paused mid-poll keeps its baseline**, so the changes that poll saw are found
  again after resume rather than absorbed silently. `done`
- **P4.14 Backoff state lives in memory**, so a restart while rate limited starts at normal speed.
  → Revisit with X3 in Phase 8: persist the global backoff level and the last request time. `proposed`
- **P4.15 Poll failures are reported once per streak** (at the 5th in a row), never repeatedly. An
  unexpected exception from an adapter is logged at ERROR and counted as a failure: the loop
  survives anything except cancellation. `done`

## Phase 5 — notifier and logging (C7)

- **P5.1 Redact when a record is created.** A LogRecord factory formats and redacts every message,
  turns any exception into redacted text and drops the raw exception object, which may hold a
  token-bearing URL. This covers every logger (httpx too), every handler and pytest's `caplog`.
  Handler filters repeat it as a second layer, and httpx/httpcore are set to WARNING. Exact secrets
  are registered (`register_secret`: the bot token now, the profile path in Phase 7), and patterns
  catch token, cookie, Authorization, `?token=` and API-key shapes. `done`
- **P5.2 Errors never carry the URL.** The Telegram client turns httpx errors into a sanitised
  `NotificationFailed` (status or error class only, no chained exception). It is tested *without*
  log redaction, so the exception is clean wherever it ends up. `done`
- **P5.3** `parse_mode=HTML`, escaping everything from the config or the site; only our own `<b>`
  and `<code>` tags remain. Messages are capped at 4096 characters, cut on a line boundary, and
  free-text reasons are clipped. `done`
- **P5.4 At most once, never lost.** `DedupNotifier` records an event id only *after* delivery, so a
  failed send is retried next time and a crash at worst repeats one message. It never raises. The
  daily heartbeat id is per local day; a manual `showwatch heartbeat` always sends. `done`
- **P5.5 Retries:** Telegram 5xx and network errors get the first try plus 3 retries (1, 2, 4 s on
  the injected clock). A 429 waits `retry_after` (capped at 60 s). Any other 4xx fails at once. `done`
- **P5.6 Hold messages never overstate the time:** minutes left are rounded down. `done`
- **P3.4** (from Phase 3) The countdown re-sent after a restart needs its own event id (for example
  `<attempt>:held:recovered:<boot time>`), or dedup will swallow it. → The orchestrator builds that id
  in Phase 8. `proposed`

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
