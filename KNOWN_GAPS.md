# EdgePulse — Known Gaps

_As of 2026-04-24 02:10 UTC, following audit session._
_Updated 2026-04-24 02:34 UTC — three fixes applied and verified._
_Updated 2026-04-24 04:30 UTC — entry/exit strategy audit, four more fixes patched._
_Updated 2026-04-24 04:45 UTC — post-deploy observations added below._
_Updated 2026-04-24 04:48 UTC — Item 11 (metrics port) fixed and verified._
_Updated 2026-04-24 04:50 UTC — Item 9 (kelly_calib column mismatch) fixed and verified._
_Updated 2026-04-24 04:55 UTC — Audit #5 (edge_threshold × 0.7 silent discount) resolved via Option A (drop multiplier, keep operator override at 0.41)._
_Updated 2026-04-24 05:10 UTC — full second-round audit across advisor, arb engine, BTC strategy, and FOMC model. Findings recorded below under "Second-round audit"._
_Updated 2026-04-24 05:20 UTC — all 5 CRITICAL items from second-round audit patched (commit 9bbabaf)._
_Updated 2026-04-24 05:25 UTC — 9 of 10 HIGH items patched (commits 60a40e4, 8be6934). Adv #2 deferred._
_Updated 2026-04-24 05:30 UTC — MEDIUM/LOW sweep (commit 8e7218d). 4 MEDIUM fixed or resolved as not-a-bug; 1 deferred. 3 LOW fixed; 1 already gated._
_Updated 2026-04-24 05:45 UTC — third-round full audit complete. 5 CRITICALs patched (commits e7efb9b, 2d4cf74, dee319c). 2 subagent CRITICALs were false positives (C4 schema validation, C5 earnings NO fee — both verified as already-correct against actual code)._

## Silently broken — needs fix

- ~~**Item 9 — Per-strategy Kelly calibration multiplier.**~~
  **FIXED 2026-04-24 04:48 UTC.** `ep_kelly_calib.py:_compute_calibration`
  had three column-name mismatches against the `terminal_trades` view:
  `model_source`→`strategy`, `pnl_cents`→`realized_pnl_cents`,
  `closed_at`→`exited_at`. All three renamed. Both queries now sit in
  separate try/except blocks so a strategy-query failure can no longer
  wipe out bucket calibration. `row["model_source"]` consumer at line
  185 also renamed to `row["strategy"]`.

  Post-fix verified via live DB dry-run:
  ```
  bucket_kelly: {}
  strat_mult:   {'fomc_butterfly_arb': 1.0, 'noaa_nws+open_meteo': 1.0}
  ```
  Both strategies return multiplier 1.0 because neither has reached
  the `n >= 20` threshold for applying a boost/penalty (currently 11
  and 10 samples respectively). Redis `ep:kelly_calib:strategy` key
  now populated for dashboard visibility. Log output shows the
  previously-loud WARNING replaced by a single INFO per tick
  ("no buckets with sufficient data — using configured defaults").

  _Note on historical data:_ `fomc_butterfly_arb` has 9% win rate over
  11 samples. Once it crosses the n>=20 threshold (it won't — Item 1
  disabled butterfly signal generation, so no new samples accumulate),
  its multiplier would drop to 0.60. Good — the empirical calibration
  is now live and would catch a similar future misfire.

- ~~**Item 11 — Metrics port 9091 double-bind on every restart.**~~
  **FIXED 2026-04-24 04:48 UTC.** `METRICS_PORT=9091` commented out
  in both `/root/EdgePulse/.env` and `/etc/edgepulse/edgepulse.env`
  with an inline warning pointing here. Each service now uses its
  own default (intel 9091 / exec 9092). Post-restart verified:
  `ss -tlnp` shows both ports bound, `curl
  http://127.0.0.1:9092/metrics` returns 6437 bytes. Prometheus
  already has scrape targets for both ports, so exec metrics start
  flowing immediately. **Note:** this fix lives in `.env` which is
  gitignored — if `.env` is ever rebuilt from scratch (eg setup
  script), make sure `METRICS_PORT` stays commented out.

## Not a gap (documented to avoid re-reporting)

- **Orphan-order reconciliation on exec startup.** Normal behavior.
  `_reconcile_orphan_orders` in ep_exec.py runs at service start and
  pulls any resting Kalshi orders into Redis if they have no record.
  The "N orphan(s) restored to Redis" WARNING at each restart is
  informational — it is not a bug. Count will vary with how many
  resting orders existed when the service last crashed / restarted.

## Verified working

- **Item 1** — Butterfly arb removal. Live evidence: zero butterfly
  signals in ep:signals post-deploy.
- **Item 6** — Dynamic FOMC model_source label. Live evidence:
  signal at 01:48:45 UTC showed `model_source="fedwatch+tbill_term"`.
- **Item 8** — Backtest Sharpe calendar-day normalization. Verified
  via live ep_backtest run; per-strategy Sharpes changed in expected
  directions.

## Code deployed but never executed (awaiting market conditions)

- **Item 2** — Earnings IV scanner. Waiting for KXERN markets to activate.
- **Item 3** — Metaculus election source. Waiting for election markets
  meeting threshold.
- **Item 4** — BTC extreme vol floor 0.40. Waiting for vol > 5% regime.
- **Item 5** — GDP fee floor 0.08. Waiting for GDP signal window.
- **Item 7** — Election+Metaculus scanner wired. Same as item 3.
- **Item 10** — Divergence monitor. First fire expected 02:48 UTC.

## Fixes applied this session (2026-04-24)

- **Fix 1** — Long-game cap fails closed on exception. `7d3a348`
  ep_exec.py:568: `log.debug` + fall-through → `log.warning` +
  `return _rejected("LONG_GAME_CAP_ERROR")`.

- **Fix 2** — Tombstone and cut-loss keys preserved until action
  succeeds. `d5059a8` ep_exec.py:1421,1450: delete moved to after
  `cancel_and_tombstone` / `_cutloss_tickers.add`; per-ticker
  try/except added; outer handlers promoted from `log.debug` to
  `log.warning`.

- **Fix 3** — Resolution write failures now visible. `cac2db5`
  ep_exec.py:1298,1325,1348: three `except Exception: pass` blocks
  replaced with `log.error(...)` including ticker context. No
  control flow change.

## Entry/exit strategy audit (2026-04-24 ~04:30 UTC)

Full audit performed on entry gates (15 gates in `_process_signal`),
exit logic (trailing stop, cut-loss, pre-expiry tranches, TIF ratchet),
intel signal generation, and confidence scoring. Five findings; four
patched this session.

- **Fix 4 — `min_confidence` parameter was ignored in
  `fetch_signals_async`.** `kalshi_bot/strategy.py:4007` hardcoded
  `s.confidence >= 0.50` instead of using the parameter. The dashboard
  `override_min_confidence` key in `ep:config` was cosmetic — any
  signal with confidence ≥ 0.50 could trade regardless of the
  operator-set minimum. `RiskManager.approve()` in `kalshi_bot/risk.py`
  also has no confidence check, so there was no downstream fallback.
  Replaced the literal with `min_confidence`.

- **Fix 5 — Meeting-concentration gate silently skipped
  Kalshi-reconciled positions.** `_sync_positions_with_kalshi` in
  `ep_exec.py` called `positions.open(...)` without passing `meeting=`
  or `close_time=`, so PositionStore defaulted them to `""`. The gate
  at `ep_exec.py:320` does `if p.get("meeting") != sig.meeting:
  continue`, treating empty strings as non-matching. Result: positions
  adopted via the periodic sync (every 30 min) did not count toward
  any meeting's cap. Live impact observed 2026-04-24: KXFED-27APR at
  7 positions, KXFED-27JAN at 8 positions — both well past the
  4-per-meeting limit. Added `_meeting_from_ticker()` helper (handles
  KXFED, KXGDP, KXCPI, KXNFP, KXINFLATION prefixes) and plumbed
  meeting plus close_time into both the add and update paths. Existing
  empty-meeting records are backfilled on the next sync cycle.

- **Fix 6 — Strategy calibration multiplier bypassed Kelly's
  `max_contracts` cap.** `ep_exec.py:443-446` multiplied the
  already-capped `contracts` by `get_strategy_conf_mult()` (up to
  1.20×) and re-assigned without re-clamping. RiskManager's
  `max_contracts=10` ceiling could be quietly exceeded (up to 12
  contracts before the absolute 500 cap). Post-multiplier
  `contracts = min(_post_mult, risk_engine._kalshi.cfg.max_contracts)`
  added.

- **Fix 7 — `NoneType.open_position` log noise at Exec startup.**
  `kalshi_bot/executor.py:_load_paper_positions` always tried
  `self.state.open_position(...)` even though Exec explicitly passes
  `state=None` at `ep_exec.py:3319` (state lives in Redis, not a
  WebSocket-backed BotState on Exec). Result: every startup logged 48
  "State sync skipped" DEBUG lines for the real bug of calling
  `.open_position` on None. Wrapped the sync loop in `if self.state
  is not None:`. Cosmetic only (positions still load into
  `self._positions` via `json.loads` above), but the blanket
  try/except could have hidden a real bug.

## Audit findings resolved in this session

- ~~**Audit #5 — `edge_threshold * 0.7` multiplier across 9 scanner
  filters is undocumented.**~~
  **FIXED 2026-04-24 04:55 UTC (Option A).** Multiplier removed from
  all 9 filter sites in `kalshi_bot/strategy.py`. Operator's
  `override_edge_threshold` value is now interpreted literally as the
  minimum fee-adjusted EV per contract. `kalshi_bot/config.py:52`
  gained a doc comment explaining current semantics and referencing
  the historical behavior. `SYSTEM_OVERVIEW.md` threshold row
  relabeled "Min fee-adjusted EV" (was "Min edge gross").

  Effective behavior change at deploy time: operator's existing
  `override_edge_threshold=0.41` was an effective 28.7¢ EV floor
  under the old `× 0.7` math. It now acts as a literal 41¢ EV floor
  — a 43% tightening. **Operator explicitly chose to keep 0.41 rather
  than lower to 0.287**, electing for a stricter filter. Expect
  signal volume to drop; monitor `ep:signals` entry cadence over
  the next 24h.

  The old rationale (30% fee/slippage discount baked into the knob)
  is gone. If that discount turns out to have been load-bearing for
  profitability, restore it by lowering the override to the desired
  EV floor directly.

## Second-round audit (2026-04-24 ~05:10 UTC)

Deep audit of four modules not covered in the entry/exit pass:
`ep_advisor.py` (meta-controller writing to ep:config), `ep_arb.py`
(real-time Polymarket↔Kalshi arb, own systemd service), `ep_btc.py` +
`ep_coinbase.py` (BTC mean-reversion on Coinbase Advanced Trade), and
`kalshi_bot/models/fomc.py` (FOMC fair-value + confidence fusion).
Performed by parallel Explore subagents; CRITICAL findings
spot-verified against live code. Two subagent CRITICALs were false
positives (marked "downgraded" below).

### CRITICAL

All 5 CRITICAL findings patched in commit `9bbabaf` on 2026-04-24 ~05:20 UTC.

- ~~**Arb #1 — `entry_cents` convention violation for NO legs.**~~
  **FIXED.** `ep_arb.py:457` now writes `(100 - limit_cents) if
  side == "no" else limit_cents`. Invariant restored. Any NO-leg arb
  positions opened under the old code will still have inverted
  entry_cents in Redis until they exit and clear — no retroactive
  backfill applied because we'd risk corrupting in-flight trades
  whose exit logic has already compensated for the bug. Going
  forward, new NO-leg arbs are correct.

- ~~**Arb #2 — Position written to Redis before fill confirmation.**~~
  **FIXED.** `ep_arb.py` hset now includes
  `fill_confirmed=False, contracts_filled=0, pending=False` (same
  convention ep_exec uses for resting orders). ep_exec's
  `_fill_poll_loop` runs every 10s and will confirm or timeout
  arb-placed orders automatically. Also derives `meeting` from
  ticker so arb positions count toward the concentration gate.

- ~~**Arb #3 — Capital limits bypassed entirely.**~~
  **FIXED.** `ep_arb.py` now reads balance from `ep:balance`,
  computes total exposure from `ep:positions`, and skips FIRE if
  adding the trade cost would exceed `ARB_MAX_TOTAL_EXP_PCT`
  (default 0.80) of balance. Fail-open with `log.warning` if
  balance read fails — preserves latency on the arb opportunity.

- ~~**Advisor #1 — No per-cycle delta clamp on whitelisted keys.**~~
  **FIXED.** `_WHITELIST` extended to `(lo, hi, max_delta)`.
  `_validate_adjustment` now takes a `current` config dict and
  rejects adjustments whose delta from current exceeds max_delta.
  Per-key clamps: scale_factor ±0.30, kelly_fraction ±0.05,
  rsi_oversold/overbought ±5, z_threshold ±0.50, max_contracts ±3.
  Caller passes `ctx["current_config"]`.

- ~~**FOMC #1 — Confidence prior/new log prints same value twice.**~~
  **FIXED.** `kalshi_bot/models/fomc.py:2747` now captures
  `prior_conf = confidence` before the `max(...)` mutation; log
  passes `prior_conf, confidence` instead of the post-mutation
  value twice.

### HIGH

9 of 10 HIGH findings patched in commits `60a40e4` + `8be6934` on
2026-04-24 ~05:25 UTC. Remaining open: Adv #2 (deferred — low urgency
in single-operator deployment).

| # | File:Line | Status | Commit |
|---|-----------|--------|--------|
| Adv #2 | ep_advisor.py:289-291 | **OPEN** — deferred; single-operator race window is seconds and manual overrides are rare | — |
| Arb #4 | ep_arb.py | FIXED — price-age gate in `_fire()` via `ARB_MAX_PRICE_AGE_S` (default 2.0s) | 8be6934 |
| Arb #5 | ep_arb.py | FIXED — hset wrapped in try/except with DELETE order on failure | 8be6934 |
| Arb #6 | ep_arb.py | FIXED — hexists re-check after post() returns; cancel + abort on race | 8be6934 |
| Arb #7 | ep_arb.py.__init__ | DEFERRED — ep_exec's `_reconcile_orphan_orders` already covers any resting Kalshi order regardless of which service placed it; redundant work not worth the code duplication. Noted inline in ep_arb.py | 8be6934 (doc) |
| BTC #1 | ep_risk.py:118 | FIXED — imports `_COINBASE_TAKER_FEE` from env instead of hardcoded 0.006 | 60a40e4 |
| BTC #2 | ep_btc.py:615,669 | FIXED — confidence floor applied after vol_mult; ceiling tightened to 0.99 | 60a40e4 |
| BTC #4 | ep_btc.py:337 | FIXED (doc-only) — `_z_score` docstring now notes 100-min window and the threshold dependency. Subagent's "1-hour" claim was inferred from my briefing, not from code | 60a40e4 |
| BTC #5 | ep_btc.py:615,669 | FIXED — `insufficient_data` vol regime now returns 0.5 (was 1.0) | 60a40e4 |
| FOMC #3 | kalshi_bot/models/fomc.py:2794 | FIXED — hard floor 0.50 / ceiling 0.99 applied immediately before MeetingProbs write | 60a40e4 |

### MEDIUM

4 of 5 MEDIUM items resolved (patched or correctly identified as
not-a-bug). Commit `8e7218d` on 2026-04-24 ~05:30 UTC.

| # | Status | Notes |
|---|--------|-------|
| Adv #6 | FIXED (8e7218d) | `recent_span_days` and `baseline_span_days` added to strategy_health result. Advisor can now distinguish an active strategy from a dead one with the same n. |
| Adv #7 | COVERED | Already addressed by Advisor #1 CRITICAL fix (delta clamp) in commit 9bbabaf. No separate patch needed. |
| BTC #7 | NOT A BUG | `pnl_half` log at ep_exec.py:1944 fires once at tranche-1 time and uses that tick's move_cents correctly. Subagent misread the control flow. Do not re-report. |
| BTC #8 | CALIBRATION CHOICE | Funding-rate skip threshold (±0.0015) tighter than adj thresholds (±0.0005 / ±0.0010) is a deliberate step function, not a correctness bug. Re-evaluate only with P&L evidence. |
| FOMC #4 | EDGE CASE | Staleness boundary at exactly 30 min (age >= 1800) fires at a single instant per cycle. Docstring is ambiguous about inclusive vs exclusive. Not worth changing. |

### LOW

3 of 4 LOW items resolved. Commit `8e7218d`.

| # | Status | Notes |
|---|--------|-------|
| Adv #5 | FIXED (8e7218d) | `cfg_raw` normalized to str keys and values immediately after hgetall. Downstream lookups no longer need the brittle bytes/str fallback. |
| Adv #8 | FIXED (8e7218d) | Calibration-skip reasons now printed when `yes_gate`/`stop_days` fall back to defaults. Includes the `note` field from each calibrator. |
| BTC #9 | FIXED (8e7218d) | Deribit skew payload `ts` compared against wall clock; adjustment skipped if > 300s old. Tightens beyond the 600s Redis TTL. |
| FOMC #5 | ALREADY GATED | `data_quality="fallback_only"` signals are already blocked at ep_intel.py:2617 unless edge >= FALLBACK_ONLY_EDGE_THRESHOLD (0.25). Subagent missed the downstream gate. Do not re-report. |

### Downgraded / false positives (do not re-report)

- ~~**BTC #3** — claimed break-even stop broken for SHORT side.~~
  Actually correct. `ep_exec.py:1980-1981`:
  `side == "sell" and current_cents > be_cents → exit` fires when
  a SHORT is losing. Symmetric with the BUY case. Not a bug.

- ~~**FOMC #1 (regime HOLD sign error)**~~ Not actually a bug.
  `kalshi_bot/models/fomc.py:319-322` leaves HOLD unchanged while
  multiplying CUT by `easing_mult`, then renormalizes. Standard
  Bayesian likelihood update; HOLD's share does decrease via the
  renormalization. Subagent wanted a more aggressive HOLD discount,
  which is a calibration choice, not a correctness bug.

### Noteworthy discoveries

- **Advisor CANNOT write `override_edge_threshold` or
  `override_min_confidence`.** The `_WHITELIST` at
  `ep_advisor.py:76-84` only permits: `llm_scale_factor`,
  `llm_kelly_fraction`, `llm_rsi_oversold`, `llm_rsi_overbought`,
  `llm_z_threshold`, `llm_max_contracts`, `HALT_TRADING`. So the
  current `override_edge_threshold=0.41` came from the operator or
  the dashboard, NOT the advisor. Today's tactical choice to keep
  0.41 after Audit #5 is safe from advisor rewriting.

- **Entire ep_arb.py lacks any fill-confirmation loop.** Unlike
  ep_exec.py which has `_fill_poll_loop` running every 10s to
  confirm/timeout resting orders, ep_arb.py fires and forgets.
  Any unfilled arb order stays in Redis indefinitely (or until
  the 4-hour timeout in ep_exec's poll, if that even runs on
  ep_arb-written positions — needs verification).

### Recommended patch order

1. **Arb #1 + #2** (one commit) — live arb records recording P&L on wrong basis. Highest blast radius.
2. **Arb #3** — capital cap. Prevents a pathological market state.
3. **Advisor #1** — delta clamp. Cheap safety net against LLM weirdness.
4. **FOMC #1** — one-line log fix.
5. Rest of HIGH findings batched or individually.

## Third-round audit (2026-04-24 ~05:45 UTC)

Full module-level audit across 8 targets: ep_intel.py, strategy.py
scanners, ep_ob_depth.py, ep_econ_release.py, ep_datasources.py,
ep_bus.py+ep_schema.py, kalshi_bot/client.py+auth.py, and three
supplementary feeds (polymarket, predictit, fed_sentiment). Performed
by 8 parallel Explore subagents with critical claims spot-verified
against live code.

### CRITICAL — all 5 real findings patched

- ~~**C1 — POST /portfolio/orders retried on timeout.**~~ **FIXED
  (commit 2d4cf74).** kalshi_bot/client.py:69-100 retry loop was
  method-agnostic; a timeout on POST after Kalshi received the order
  would double-place on retry. Added `_NON_IDEMPOTENT_POST_PATHS`
  tuple (currently just `/portfolio/orders`); when POST matches,
  `effective_retries=0` and Timeout/ConnectionError raise immediately
  so ep_exec's orphan reconciliation can reconcile instead of
  double-placing.

- ~~**C2 — Shared requests.Session across threads.**~~ **FIXED
  (commit 2d4cf74).** kalshi_bot/client.py:61 used a single
  `requests.Session()` for sync calls, which is not thread-safe. Since
  sync methods are called via `asyncio.to_thread` from ep_arb, ep_exec,
  and ep_ob_depth, the shared connection pool corrupted under load.
  Moved to `threading.local`; each thread gets its own Session
  lazily via `_get_session()`.

- ~~**C3 — ep_ob_depth bypasses every entry gate.**~~ **FIXED
  (commit dee319c).** The OB depth service was placing Kalshi orders
  directly via `POST /portfolio/orders` — no ep:positions dedup, no
  balance/exposure check, no Kelly, no halt respect, no ep:positions
  write. Configured confidence multipliers were dead code. Refactored:
  `_place_order` → `_publish_signal`, emits SignalMessage-shaped
  payload to ep:signals (15s TTL); ep_exec consumes and applies all
  standard gates. Meeting tag derived from ticker so concentration gate
  counts OB-triggered positions.

- ~~**C6 — Bracket stored when both legs fail.**~~ **FIXED (commit
  e7efb9b).** ep_econ_release.py:308-336 added a pending Bracket to
  self._brackets even if both YES and NO order placements failed. Now:
  if neither leg got an order_id, skip the bracket store and log a
  warning.

- ~~**C7 — Momentum orders bypass override_edge_threshold.**~~
  **FIXED (commit e7efb9b).** ep_econ_release.py:_add_momentum placed
  orders without checking the operator's EV floor. Now reads
  `override_edge_threshold` from ep:config at the top of the function,
  uses a conservative `σ × 5¢` edge proxy (calibrated to CPI 1σ ≈ 5¢
  move), and skips the momentum if the proxy doesn't clear the
  operator's threshold. Fail-open on Redis error with debug log.

### CRITICAL — false positives (don't re-report)

- ~~**C4 — `from_redis` skips `__post_init__` validation.**~~
  **NOT A BUG.** Python dataclass `__init__` auto-calls `__post_init__`.
  `cls(**known)` at ep_schema.py:144 triggers validation correctly.
  Verified with a live test: constructing SignalMessage with
  `confidence=1.5` raises `ValueError` as expected.

- ~~**C5 — scan_earnings_markets NO-side fee math inverted.**~~
  **NOT A BUG.** `_fee_adjusted_edge(fair_value, market_price, side)` at
  kalshi_bot/strategy.py:164 handles NO side internally: when `side="no"`,
  it uses `(1-fair_value) × market_price × (1-fee) - fair_value × (1-market_price)`.
  Scanner passes both fair_value and market_price in YES-space (verified).
  Computed EV matches the correct formula for a NO bet. Subagent did
  not read the helper's NO-branch.

### HIGH findings (13 items, unpatched)

| # | File:Line | Issue | Priority |
|---|-----------|-------|----------|
| H1 | ep_intel.py:1609-1624 | `markets_last_scan` not updated on scan failure → retry every cycle (10× normal rate) | Low-risk; retries are at most 10/2h, not catastrophic |
| H2 | strategy.py:3176, 3260 | UNRATE / CPI coherence scanners filter strike > 3.75 / < 4.00 hardcoded — dead at current ~3.75% Fed rate | Dead scanners, not actively harmful |
| H3 | strategy.py:1367 | Weather "less" strike_type double-inverts fair_value + price — edge sign flipped for below-threshold markets | NEEDS VERIFICATION — subagents wrong twice this round, verify before patching |
| H4 | strategy.py:752 vs 914 | Signal.fair_value convention inconsistent between FOMC and other scanners | Documentation/consistency fix |
| H5 | ep_bus.py:94-105 | publish_signal XADD no try/except — Redis blip drops signals silently | Safety improvement |
| H6 | ep_bus.py:123-151 | PEL drain exits at count=100 batch boundary | Edge case on massive backlog |
| H7 | ep_bus.py:182-198 | Consumer-group recreate at id="0" replays entire stream → possible order flood on NOGROUP recovery | Rare but catastrophic-if-it-fires |
| H8 | ep_datasources.py:109-127 | No 429 handling on FRED | Silent stale cache on rate-limit |
| H9 | ep_datasources.py:340-372 | Deribit midnight-UTC rollover blindness (60s/day) | Annoying but contained |
| H10 | ep_econ_release.py:449-453 | Report-month derivation wrong for mid-month CPI release | CPI confirmation could match wrong data month |
| H11 | ep_econ_release.py | No DST handling on 8:30 ET release times | Up to 60min drift at DST transitions |
| H12 | ep_polymarket.py:172 vs ep_predictit.py:451 | Fee asymmetry (flat 2¢ vs 1¢) | Ranking consistency |
| H13 | ep_polymarket.py:231-249 | Strike substring matching can mis-match series | Keyword + strike co-requirement needed |

### MEDIUM (18 items, unpatched)

Summarized in audit report; includes FOMC fair_value convention,
ep_ob_depth geometry comment, ep_econ_release σ-threshold tuning,
schema version validation, BTC cross-exchange partial-data handling,
Fed sentiment date filter, etc. Recorded for later triage.

### LOW (12 items, unpatched)

Cosmetic/hygiene; client async-client-per-request (perf), weak
private-key permission check, SR3 zombie fetcher, etc.

### Verification discipline

Two subagent CRITICALs were false positives (C4 schema, C5 earnings).
Both caught by spot-checking against live code before patching. When
agents claim CRITICAL without line-number evidence that unambiguously
demonstrates the bug, verify before applying.

## Infrastructure gaps (separate workstream)

- No feature-health heartbeat — features can silently stop working
  without detection. Items 9 (broken) and the old divergence monitor
  (dead for weeks) are examples. Proposed: per-feature
  `ep:feature_health:{name}` key updated on every successful
  iteration, plus a health checker that flags stale entries.
- `except Exception` hygiene sweep partially done (3 critical-path
  fixes above). ~280 remaining blocks in ep_*.py / kalshi_bot/
  still unaudited.
- 33 commits ahead of origin/v2-single-box with no remote backup.
  HTTPS credentials need resolving.
- No automated test framework. Every new feature currently requires
  manual in-production verification.

## Do not build on top of the unverified items until they've fired
## at least once and been spot-checked.
