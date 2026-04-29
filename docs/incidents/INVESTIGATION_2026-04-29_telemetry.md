# EdgePulse Strategy Investigation — 2026-04-29

## Original task
Review four research sources on Kalshi/Polymarket microstructure and calibration, then suggest strategy changes for the EdgePulse bot. Investigation expanded into the bot's own data, executor, and risk gates because the literature recommendations didn't match the empirical picture.

## Sources reviewed

| # | Source | One-line summary |
|---|---|---|
| 1 | arXiv:2602.19520 — Le, "Decomposing Crowd Wisdom" | Calibration decomposition on 292M Kalshi+Polymarket trades. Politics underconfident, weather overconfident at <1wk horizons. Recalibration formula: `p* = p^θ / (p^θ + (1-p)^θ)` |
| 2 | arXiv:2604.24366 — Dubach, "The Anatomy of a Decentralized Prediction Market" | Polymarket microstructure. Trade direction inference 59% accurate from public feed; longshot spread premium; depth decay near expiration. Mostly Polymarket-specific. |
| 3 | arXiv:2604.24147 — Nechepurenko, "Price as Focal Point" | Prediction markets as coordination devices; Signal Credibility Index. Heavily political; minimal transfer to weather/FOMC. |
| 4 | jbecker.dev — Becker, "The Microstructure of Wealth Transfer in Prediction Markets" (Jan 2026) | 72.1M Kalshi trades. Takers lose ~1.12%/trade, makers gain ~1.12%; gap emerged Q2 2024. Per-category gaps: Finance 0.17pp, Politics 1.02, Weather 2.57, Sports 2.23, Crypto 2.69, Entertainment 4.79, Media 7.28, World Events 7.32. NO outperforms YES at 69 of 99 price levels; YES at 1¢ has -41% historical EV vs NO at 1¢ +23%. Maker side selection is noise (Cohen's d = 0.02-0.03). |

## Initial literature-derived recommendation hierarchy

Pre-investigation, based on Le + Becker:
- T1: Apply Le's recalibration to weather (largest signal-mix share)
- T1: Audit FOMC/GDP for losses; Becker says Finance is most efficient (0.17pp gap)
- T2: Investigate maker/taker mix (executor.py uses limit orders, semantics depend on price source)
- T2: Pull resolutions.db for ground-truth PnL
- T3: Buy NO at 91-99¢ instead of YES at 1-9¢ (Becker's tail rule)

Most of these collapsed under inspection. See "What changed" at the end.

## Investigation findings

### 1. trades.csv FIFO-matched analysis (132 live entry/exit pairs)

| Category | n | Win rate* | Total PnL | ROI |
|---|---|---|---|---|
| Weather | 80 | 63.7% | +$183.10 | +155.6% |
| FOMC | 43 | 39.5% | -$6.70 | -12.1% |
| GDP | 8 | 0.0% | -$1.30 | -12.2% |
| Arb | 1 | — | — | — |
| **Total live** | **132** | **52.3%** | **+$177.10** | **+96.2%** |

\* "Win rate" = exit price moved in our favor; not a forecast-accuracy metric.

**Becker's prediction was inverted on EdgePulse's data:** weather (highest predicted drag at 2.57pp) is the moneymaker; FOMC (predicted-efficient at 0.17pp) is losing.

**Becker's NO-vs-YES tail rule was disconfirmed:** YES@01-10¢ live trades returned +$156.50 on $11.90 of cost (40 pairs, 70% win rate). The model is finding genuinely-undervalued longshots; flipping to NO would have been catastrophic.

### 2. SQLite resolutions.db is broken

`output/resolutions.db` (file mtime 2026-04-23 12:52):
- `resolutions` table schema: rich (resolved_yes INTEGER, price_48h, price_24h, was_surprise, ...)
- `ep_resolution_db.py:464-466` writes column `outcome` which doesn't exist in the live schema
- Direct test: `INSERT OR REPLACE INTO resolutions (ticker, outcome, ...) VALUES ...` returns `OperationalError: no such column: outcome`
- Exception swallowed at `ep_resolution_db.py:469-470`

`record_trade_outcome` has **no callers** in the codebase (`grep -rn "record_trade_outcome"` returns only the definition).

The 3,244 rows in `resolutions` and 266 rows in `trade_outcomes` are seed/backfill data, all dated 2026-04-15 to 2026-04-23. Live code has not written to either table since deployment.

3 sites in `ep_exec.py` call the broken `db.get_outcome` (lines 1732, 1797-1798) and silently get `None`. They have fallback paths so the bot still functions, but anything that depended on the binary outcome lookup is degraded.

The file `ep_resolution_db.py` has TWO subsystems. The CSV-based functions (`get_performance_summary`, `_load_completed_trades`, `print_performance_report`, `get_rolling_strategy_health`) are used by ep_intel/ep_advisor/risk.py and work fine. Only the SQLite half is dead.

### 3. PostgreSQL is the real analytics layer

| Table | 7-day rows | Notes |
|---|---|---|
| signals | 15,535 (all-time); 310 (24h) | Working |
| executions | 15,842 (all-time); 375 (24h) | Working |
| position_history | 52 (7d) | Started writing 2026-04-23 12:52 — exactly when SQLite trade_outcomes stopped. Migration cutover. |
| terminal_trades (view) | 52 (7d) | View joins work |

**The migration to Postgres on Apr 23 is what made the SQLite path appear broken.** Pre-Apr 23 it had been written by a backfill script; post-Apr 23 nothing writes to it.

### 4. Fill rate by strategy (last 7d) — the binding constraint

Of 15,547 signals, only **779 (5%) actually filled**:
- rejected: 13,554 (87%)
- duplicate: 1,516 (10%)
- filled: 779 (5%)
- expired: 7

Per-strategy fill rate:

| Strategy | Signals | Filled | Fill % |
|---|---|---|---|
| fomc_butterfly_arb | 7,395 | 19 | 0.3% |
| gfs+noaa_hourly (weather) | 5,131 | 264 | 5.1% |
| fedwatch+tbill_term | 1,793 | 21 | 1.2% |
| fedwatch+zq+wsj | 839 | 4 | 0.5% |
| gdpnow_1.2pct | 230 | 9 | 3.9% |
| noaa_nws+open_meteo | 66 | 19 | 28.8% |

Top reject reasons: `ENTRY_FAILED_COOLDOWN` (2,940+), `LONG_GAME_CAP` (2,013+), `STOP_COOLDOWN` (1,855+), `MARKET_LIMIT` (1,764+), `RISK_GATE_KALSHI` (1,394+).

**Implication:** Becker/Le/Dubach all measure microstructure at the exchange. The bot rarely reaches the exchange. Literature recommendations are downstream of "the bot trades what its scanners emit," which is not the binding constraint.

### 5. The risk-gate cascade

`ep_exec.py:520-521`: when Kelly sizer returns 0 contracts:
```python
_entry_failed_cooldown[sig.ticker] = time.time() - (_ENTRY_FAILED_COOLDOWN_S - _ENTRY_FAILED_COOLDOWN_SHORT)
return _rejected("RISK_GATE_SIZE")
```

Sets cooldown timestamp such that ~10 minutes remain. Each subsequent signal on the same ticker within those 10 min returns `ENTRY_FAILED_COOLDOWN`. With scanners running every 120s, ~5 cooldown rejections per RISK_GATE_SIZE rejection.

**Why does Kelly size to 0?**
- Balance: $275 cash + $86 portfolio = $361 total
- 37 open positions
- `llm_kelly_fraction = 0.10` (was 0.14 at session start; advisor lowered)
- `llm_scale_factor = 0.75`
- `override_max_contracts = 5`

Typical FOMC butterfly leg costs $0.40–$0.60 per contract. With 37 positions consuming budget and these multipliers, sized contract count rounds below 1 → 0. **Sizing failures are systemic.**

### 6. Why the bot is in protective mode

`ep:config:llm_notes`:
```
Strong edge (136¢ expectancy, +$177 PnL over 30d). Recent fills -$678 vs portfolio $202
suggests drawdown pressure; reduce scale to 0.75 and kelly to 0.22 as precaution.
```

The advisor's "+$177 over 30d" matches +$175.95 in trades.csv (live, all history). Confirmed.

The advisor's "**-$678 in recent fills**" appears nowhere in the data:

| Window | Realized PnL (trades.csv) |
|---|---|
| Last 24h | -$4.49 (9 trades) |
| Last 3d | -$7.00 (21 trades) |
| Last 7d | +$166.09 (91 trades) |
| Last 14d | +$175.95 (151 trades) |

Worst single trade: -$4.03. Open-position unrealized: -$8.24. Coinbase: $0.05 (zero BTC trade activity ever — `position_history WHERE ticker LIKE 'BTC%'` returns 0 rows).

There is no realized loss anywhere matching $678.

### 7. Root cause: edge_captured is double-purposed

`ep_schema.py:200`:
```python
edge_captured: float = 0.0   # signal.edge at time of fill (P&L attribution)
```

Documented as **predicted edge at entry** (per-contract decimal, typically 0–0.3).

In practice, ep_exec.py writes it differently for entry vs exit fills:

| ep_exec.py line | Context | Formula | Unit |
|---|---|---|---|
| 1014 | Entry (arb leg) | `sig.edge - (_arb_fee / 100)` | per-contract decimal |
| 1174 | Entry (standard) | `sig.edge - (cfg.FEE_CENTS * contracts) / 100` | per-contract decimal-ish |
| 1778 | **Exit** | `_pnl_r` | **dollars** |
| 1836 | **Exit** (TP/SL) | `(pnl_cents - cfg.FEE_CENTS * _cr) / 100` | **dollars** |
| 2034 | **Exit** (T1) | `(_t1_pnl - _t1_fee) / 100` | **dollars** |
| 2173 | **Exit** (trailing) | `move_cents * half / 100` | **dollars** |
| 2502 | **Exit** (general) | `(pnl_cents - _exit_fee) / 100` | **dollars** |
| 2662 | **Exit** (late fill) | `(_lf_pnl - _lf_fee) / 100` | **dollars** |
| 2899 | **Exit** fallback | `0.0` | — |

Entries: per-contract decimal (~0–0.3). Exits: realized PnL in dollars (can be ±$50).

`llm_agent.py:255-284` sums `edge_captured` across all `status='filled'` executions as if homogeneous:
```python
if rep.get("status") == "filled":
    fills        += 1
    edge          = float(rep.get("edge_captured", 0))
    pnl_edge_sum += edge
```

Single losing exits dominate hundreds of small entry edges. Direct query against Postgres:

| Window | sum(edge_captured) |
|---|---|
| Last 24h | -$115.58 |
| Last 7d | **-$4,617.75** |

Daily breakdown shows **Apr 25 alone = -$3,392.67** of edge_captured aggregate. Sample bad row: `KXNBAGAME-26APR28ATLNYK-ATL NO at 0.01, edge_captured = -45.51`. A single fill at 1¢ on a binary contract; the field is recording dollars of realized exit loss, not per-contract entry edge.

The advisor's "-$678" is approximately a windowed sum of this broken metric. Not a real loss. The bot is throttled to 5% fill rate based on telemetry that doesn't measure what it claims to measure.

### 8. Three-way data integrity mismatch

Three independent sources disagree on 7-day PnL:

| Source | 7-day PnL | n |
|---|---|---|
| trades.csv FIFO pairs (live) | +$166 | 91 closed pairs |
| position_history (Postgres) | ~+$1 | 12 non-cancellation rows |
| sum(edge_captured) | -$4,617 | 805 fills (mixed entry+exit) |

trades.csv has 91 closed pairs but position_history has only 12 in the same window — 7× mismatch. Possible causes: weather top-up logic bypassing position_history, Redis state edge cases, or trades.csv double-counting via FIFO matching across top-ups. **At least two of these three logs aren't trustworthy.** Separate investigation needed.

### 9. NBA market activity flagged

The bot is filling KXNBAGAME-* (Atlanta vs NY Knicks, Philadelphia vs Boston) contracts despite the user's mental model being weather/FOMC/GDP only. Source unknown — could be undocumented scanner, polymarket arb leg routing, or signal-routing bug. Worth investigating but not the load-bearing finding here.

## Updated recommendation hierarchy

**Tier 0 — fix telemetry before changing risk posture:**
1. **Fix `edge_captured` semantics.** Either:
   - (Cleanest) Split into two fields on `ExecutionReport`: `predicted_edge` (entry only) and `realized_pnl_cents` (exit only). Update all 9 writers in ep_exec.py and Postgres schema.
   - (Minimal) Change `llm_agent.py:255` to read realized PnL from `position_history.realized_pnl_cents` instead of summing `edge_captured`. Filter the aggregate to entries only OR drop it entirely.
2. **Document in the advisor prompt** that `recent_pnl_edge` is unreliable until fixed; force the LLM to use `position_history` data for drawdown assessment.

**Tier 1 — once telemetry is fixed:**
3. Clear protective overrides in `ep:config`:
   - Restore `llm_kelly_fraction` from 0.10 to 0.20-0.25 (or whatever advisor's pre-protective default was)
   - Remove `override_max_contracts=5`
   - Restore `llm_scale_factor` to 1.0
   - Verify with the advisor's next run that the metric reads correctly before unlocking fully
4. Reconcile trades.csv vs position_history (separate audit).

**Tier 2 — strategy-relevant once fill rate is reasonable:**
5. **Disable or retune fomc_butterfly_arb scanner.** 7,395 signals → 19 fills (0.3% fill rate) is generating log noise. Either rate-limit the scanner or fix the cooldown propagation that throws away most attempts.
6. **Investigate NBA market fills** to understand the source.
7. Once 30+ closed positions per category are in position_history, redo the weather/FOMC/GDP per-category analysis on clean data and decide whether the literature-derived recommendations apply.

**Tier 3 — defer:**
8. Le's recalibration. Current weather scanner is positive; recalibration is risky to apply preemptively.
9. Becker's NO-favorite tail rule. Disconfirmed by EdgePulse's data.
10. Dubach (Polymarket-specific) and Nechepurenko (politics-specific) are not actionable here.

**Tier 4 — housekeeping:**
11. Surgically remove the broken SQLite half of `ep_resolution_db.py` (`ResolutionDB` class, `poll_resolutions_loop`, the 3 dead call sites in ep_exec.py at lines 1732, 1797-1798, 3880). Keep CSV-based functions. Do NOT delete the file.

## What changed from the initial recommendation hierarchy

The initial literature-derived plan would have had us tweaking calibration on a bot that:
- Mostly doesn't fill
- Is mostly throttled by phantom-drawdown protective mode
- Has a fundamentally broken telemetry field underneath the LLM advisor

None of the literature-suggested optimizations (recalibration, NO-vs-YES tail rule, depth-decay timing, focal-point reflexivity) are the binding constraint. They become relevant only after the telemetry is fixed and fill rate normalizes.

## Files / data referenced

- `/root/EdgePulse/output/trades.csv` — 2,431 rows, 132 live entry/exit pairs
- `/root/EdgePulse/output/resolutions.db` — broken; rich schema, code writes wrong columns
- `/root/EdgePulse/kalshi_bot/executor.py` — limit orders only (Kalshi has no market orders)
- `/root/EdgePulse/kalshi_bot/strategy.py:163` — `market_price` documented as mid; line 474 uses `yes_ask_low` for one path
- `/root/EdgePulse/ep_exec.py` — 9 writers of `edge_captured` (lines 1014, 1174, 1778, 1836, 2034, 2173, 2502, 2662, 2899)
- `/root/EdgePulse/ep_resolution_db.py` — dead SQLite half + working CSV half
- `/root/EdgePulse/llm_agent.py:255-284` — broken aggregate metric
- `/root/EdgePulse/ep_schema.py:200` — `edge_captured` docstring (predicted edge at fill)
- Postgres `position_history`, `executions`, `signals`, `terminal_trades` — working analytics layer

## Concrete next actions if user resumes

1. (15 min) Read the 9 `edge_captured` writers in ep_exec.py and decide minimal vs comprehensive fix.
2. (5 min) Patch llm_agent.py to either filter to entries or read from position_history. Test next advisor run.
3. (5 min, only after #2 confirms metric is fixed) Clear protective overrides in `ep:config`. Verify fill rate rises.
4. (separate session) Reconcile trades.csv vs position_history mismatch.
5. (separate session) Investigate NBA market fills.
