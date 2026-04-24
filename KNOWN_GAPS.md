# EdgePulse — Known Gaps

_As of 2026-04-24 02:10 UTC, following audit session._
_Updated 2026-04-24 02:34 UTC — three fixes applied and verified._
_Updated 2026-04-24 04:30 UTC — entry/exit strategy audit, four more fixes patched._
_Updated 2026-04-24 04:45 UTC — post-deploy observations added below._
_Updated 2026-04-24 04:48 UTC — Item 11 (metrics port) fixed and verified._

## Silently broken — needs fix

- **Item 9 — Per-strategy Kelly calibration multiplier.**
  Code deployed in ep_exec.py `_process_signal`. The
  `get_strategy_conf_mult()` function reads from `_strategy_conf_mult`
  dict which is populated by `kelly_calib_loop` → `_compute_calibration`.
  `_compute_calibration` queries the `terminal_trades` VIEW (exists).
  First query (bucket Kelly) works. Second query (per-strategy mult)
  fails with `column "model_source" does not exist` — the view has
  `strategy`, not `model_source`. Also: `pnl_cents` → `realized_pnl_cents`,
  `closed_at` → `exited_at`. Three column mismatches. Both queries share
  one try/except so the second failure kills both. Result: every signal
  gets multiplier 1.0 (no-op). Feature is cosmetic until fixed.

  _Post-deploy observation (2026-04-24 04:38 UTC):_ warning fires on
  every intel cycle (~9 occurrences in the last 4h). Previously was
  quieter because it logged at DEBUG during certain code paths; now
  a WARNING on every kelly_calib tick. Not a new break — the query
  always failed — just now more visible.

  Fix: in the second query, rename `model_source`→`strategy`,
  `pnl_cents`→`realized_pnl_cents`, `closed_at`→`exited_at`. Split
  the two queries into separate try/except blocks so bucket calibration
  survives a per-strategy query failure.

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

## Audit findings still unpatched

- **Audit #5 — `edge_threshold * 0.7` multiplier across 9 scanner
  filters is undocumented.** `kalshi_bot/strategy.py:3822, 3848, 3859,
  3870, 3884, 3895, 3928, 3944, 3955` all compare
  `fee_adjusted_edge >= edge_threshold * 0.7`. With
  `override_edge_threshold=0.41` that means the effective filter is
  28.7¢, not 41¢ as the dashboard suggests. Either rename the override
  to reflect actual behavior or drop the 0.7 multiplier — decision
  deferred pending discussion with operator about intended semantics.

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
