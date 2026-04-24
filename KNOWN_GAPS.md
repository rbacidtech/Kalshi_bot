# EdgePulse — Known Gaps

_As of 2026-04-24 02:10 UTC, following audit session._
_Updated 2026-04-24 02:34 UTC — three fixes applied and verified._

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

  Fix: in the second query, rename `model_source`→`strategy`,
  `pnl_cents`→`realized_pnl_cents`, `closed_at`→`exited_at`. Split
  the two queries into separate try/except blocks so bucket calibration
  survives a per-strategy query failure.

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
