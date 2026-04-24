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

| # | File:Line | Issue | Fix idea |
|---|-----------|-------|----------|
| Adv #2 | ep_advisor.py:289-291 | Config writes don't use WATCH/MULTI; races with operator writes | Single-operator in practice, low urgency; add check-and-set if multi-operator adoption comes later |
| Arb #4 | ep_arb.py:362-389 | `poly_ts`/`kalshi_ts` read but never compared to wall clock; stale prices can fire an arb after divergence closed | `if (now - pair.poly_ts) > MAX_AGE_MS: skip` |
| Arb #5 | ep_arb.py:444-468 | Exception after order_id obtained but before Redis write → orphan on Kalshi | Try/except around hset with DELETE of order_id on failure |
| Arb #6 | ep_arb.py:422 | `hexists` then `hset` is not atomic — race vs ep_exec writing to same ticker | Redis WATCH/MULTI or ep_exec's `_arb_legs_in_progress` set (the latter requires ep_arb to hit that code path) |
| Arb #7 | ep_arb.py.__init__ | No startup reconciliation of arb-placed orders against Kalshi portfolio | Mirror exec's `_reconcile_orphan_orders` path on arb startup |
| BTC #1 | ep_risk.py:118 | Hardcoded 0.6% fee in sizing; doesn't follow `COINBASE_TAKER_FEE` env var | Import the env var at top of ep_risk |
| BTC #2 | ep_btc.py:615,669 | Confidence floor applied before vol multiplier; extreme-vol regime can produce conf < 0.10 | Move `max(0.10, ...)` to after the vol_mult multiplication |
| BTC #4 | ep_btc.py:337,506 | Z-score period=20 (100 min) but docstring claims "1-hour lookback" — 40-min drift | Either change period to 12 or update docstring; threshold z=1.8 is calibrated to current behavior so a numeric change needs recalibration |
| BTC #5 | ep_btc.py | `insufficient_data` vol regime gets mult=1.0 — noisy z from sparse buffer fires at full confidence | Return 0.5 for insufficient_data, or require `len(closes) >= 50` before first signal |
| FOMC #3 | kalshi_bot/models/fomc.py:2759-2797 | No hard floor/ceiling on confidence before `MeetingProbs` write — can drift below 0.50 or above 0.99 | `confidence = max(0.50, min(0.99, confidence))` before line 2793 |

### MEDIUM

- **Adv #6** — `ep_advisor.py:267` hardcodes `recent_n=20`, `baseline_n=50` with no time-span sanity check. In a low-trade-cadence regime, "recent 20 trades" could span 10 days.
- **Adv #7** — `ep_advisor.py:362-373` validates only the absolute value, not its magnitude relative to the current config value. Confidence-weighted delta constraint would catch large confidence-threshold-just-above-0.80 jumps.
- **BTC #7** — `ep_exec.py:1944` logs `pnl_half` using `move_cents * half`, but if a second tranche fires soon the logged pnl becomes stale. Reporting-only.
- **BTC #8** — `ep_btc.py:586,590` hardcoded funding-rate skip threshold (±0.0015) is 50–150% tighter than the adjustment thresholds (±0.0005 / ±0.0010). Creates a small skew at the boundary.
- **FOMC #4** — `kalshi_bot/models/fomc.py:2920` staleness boundary at exactly 30 min gets penalized (age >= 1_800). Whether this is a bug depends on whether the 30-min threshold is meant to be inclusive.

### LOW

- **Adv #8** — `ep_advisor.py:287-291` calibration-skip reasons not logged; operator can't tell why `override_min_yes_entry_price` isn't being updated.
- **Adv #5** — `ep_advisor.py:304` bytes/str fallback on hgetall keys is brittle. Normalize keys after read.
- **BTC #9** — Deribit skew applied without a freshness check on the feed.
- **FOMC #5** — `data_quality="fallback_only"` flag is set correctly but not enforced downstream (ep_exec doesn't check it). Could sneak a low-confidence FRED-only signal through.

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
