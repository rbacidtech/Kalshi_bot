# EdgePulse — Known Gaps

_As of 2026-04-24 02:10 UTC, following audit session._

## Silently broken — needs fix

- **Item 9 — Per-strategy Kelly calibration multiplier.**
  Code deployed in ep_exec.py `_process_signal`. The
  `get_strategy_conf_mult()` function reads from `_strategy_conf_mult`
  dict which is populated by `kelly_calib_loop` → `_compute_calibration`.
  `_compute_calibration` queries a `terminal_trades` table that does
  not exist in Postgres. The asyncpg exception is caught silently
  and `{}` is returned. Result: every signal gets multiplier 1.0
  (no-op). Feature is cosmetic until fixed.

  Fix: rewrite `_compute_calibration` to read from trades.csv
  (same source as divergence monitor) OR join ep:executions entries
  and exits from Postgres.

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

## Infrastructure gaps (separate workstream)

- No feature-health heartbeat — features can silently stop working
  without detection. Items 9 (broken) and the old divergence monitor
  (dead for weeks) are examples. Proposed: per-feature
  `ep:feature_health:{name}` key updated on every successful
  iteration, plus a health checker that flags stale entries.
- No `except Exception` hygiene sweep done — multiple try/except
  blocks swallow errors silently. Needs `grep -rn "except Exception"
  ep_*.py kalshi_bot/` audit with log lines added to each.
- 30 commits ahead of origin/v2-single-box with no remote backup.
  HTTPS credentials need resolving.
- No automated test framework. Every new feature currently requires
  manual in-production verification.

## Do not build on top of the unverified items until they've fired
## at least once and been spot-checked.
