# EdgePulse — Known Gaps

_As of 2026-04-24 02:10 UTC, following audit session._
_Updated 2026-04-24 02:34 UTC — three fixes applied and verified._
_Updated 2026-04-24 04:30 UTC — entry/exit strategy audit, four more fixes patched._
_Updated 2026-04-24 04:45 UTC — post-deploy observations added below._
_Updated 2026-04-24 04:48 UTC — Item 11 (metrics port) fixed and verified._
_Updated 2026-04-24 04:50 UTC — Item 9 (kelly_calib column mismatch) fixed and verified._
_Updated 2026-04-24 04:55 UTC — Audit #5 (edge_threshold × 0.7 silent discount) resolved via Option A (drop multiplier, keep operator override at 0.41)._

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
