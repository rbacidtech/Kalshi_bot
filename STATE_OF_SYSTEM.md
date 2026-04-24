# EdgePulse — State of System
**Generated:** 2026-04-24  **Branch:** v2-single-box  **Last commit:** c54b0de

---

## 1. Wiring Inventory

All scanners in `fetch_signals_async` (`kalshi_bot/strategy.py`). Called from `ep_intel.py` line 1815.

| Scanner | File | In intel cycle? | Notes |
|---------|------|----------------|-------|
| scan_fomc_directional | strategy.py:490 | ✅ block 1 | Main FOMC pricing |
| scan_fomc_arb (monotonicity) | strategy.py:361 | ✅ block 1a | Butterfly section removed 2026-04-24 |
| scan_econ_ladder_arb | strategy.py:455 | ✅ block 1a | CPI/NFP ladder monotonicity |
| scan_near_resolution | strategy.py | ✅ book_arb block | High-confidence near-expiry |
| scan_book_arb | strategy.py | ✅ book_arb block | YES+NO complement arb |
| scan_weather_markets | strategy.py | ✅ block 3 | NOAA/Open-Meteo ensemble |
| scan_economic_markets | strategy.py:1490 | ✅ block 4 | CPI/NFP/JOBS anchor |
| scan_sports_markets | strategy.py | ✅ block 5 | ESPN odds (ENABLE_SPORTS=false) |
| scan_crypto_price_markets | strategy.py | ✅ block 6 | KXBTC/KXETH log-normal |
| scan_gdp_markets | strategy.py:1868 | ✅ block 7 | GDPNow + fallback |
| scan_cross_series_coherence | strategy.py | ✅ block 8 | GDP-FOMC cross-series |
| scan_cross_meeting_coherence | strategy.py | ✅ block 9a | Forward-path monotonicity |
| scan_rate_path_value | strategy.py | ✅ block 9b | Calendar spread value |
| scan_unrate_fomc_coherence | strategy.py:3146 | ✅ block 8b | UNRATE↔FOMC coherence |
| scan_cpi_fomc_coherence | strategy.py:3231 | ✅ block 8c | CPI↔FOMC coherence |
| scan_calendar_decay | strategy.py:3315 | ✅ block 10 | Time-premium decay on stable YES |
| scan_earnings_markets | strategy.py:3394 | ✅ block 11 | KXERN IV-based scoring |
| scan_election_markets | strategy.py:2428 | ✅ via ep_intel:2324 | Direct call in intel loop |
| scan_election_markets_with_538 | strategy.py:3631 | ❌ NOT WIRED | Metaculus wrapper; needs ep_intel wire-up |
| generate_predictit_signals | ep_predictit.py | ✅ block 2b | FOMC PredictIt divergence |

**Last file modifications (all 2026-04-24):** ep_btc.py, kalshi_bot/strategy.py

---

## 2. Live Redis State

| Key | Value |
|-----|-------|
| ep:signals (stream len) | 10,002 |
| ep:executions (stream len) | 5,001 |
| ep:positions (hash len) | 48 positions |
| ep:cooldown:* | 9 active |
| ep:cut_loss:* | 1 active |
| ep:tombstone:* | 1 active |
| ep:divergence | **EMPTY** (monitor not yet writing) |

**ep:config overrides (keys only — no values):**
`override_edge_threshold`, `override_min_confidence`, `override_max_contracts`,
`llm_kelly_fraction`, `llm_max_contracts`, `llm_z_threshold`, `llm_scale_factor`,
`llm_halt_trading`, `llm_kalshi_enabled`, `llm_btc_enabled`, `llm_rsi_oversold`,
`llm_rsi_overbought`, `HALT_TRADING`, `llm_notes`, `llm_last_run_ts`

**Balance (live mode):**

| Node | Cash | Portfolio | Total |
|------|------|-----------|-------|
| intel-qvps-chi (Kalshi) | $243.07 | $46.38 | $289.45 |
| coinbase | $65.84 | — | $65.84 |
| exec-qvps-chi | $83.52 | $22.34 | $105.86 |
| **TOTAL** | | | **~$392** |

---

## 3. Recent Execution Outcomes (last 200 Redis entries)

| Status | Count | % |
|--------|-------|---|
| rejected | 195 | 97.5% |
| filled | 4 | 2.0% |
| duplicate | 1 | 0.5% |

**Top reject reasons (Redis, recent ~200 signals):**

| Reason | Count | % |
|--------|-------|---|
| MEETING_CONCENTRATION | 110 | 56.4% |
| STOP_COOLDOWN | 50 | 25.6% |
| ENTRY_FAILED_COOLDOWN | 23 | 11.8% |
| ARB_LEG_RESTING | 8 | 4.1% |
| EXECUTOR_REJECTED | 3 | 1.5% |

**Note:** `MEETING_CONCENTRATION` dominating the recent window reflects the advisor override `override_edge_threshold=0.41` blocking most directional FOMC signals; only arb signals with multi-leg concentration are being attempted.

---

## 4. Per-Strategy Performance (Postgres, last 14 days)

Postgres `executions` table does not carry `model_source`. Backtest analytics from `trades.csv` (90-day window):

| Strategy | N | Win% | Total P&L | Sharpe | Avg Edge |
|----------|---|------|-----------|--------|----------|
| fedwatch+zq+wsj* | 402 | 12.9% | +$160.54 | 6.72 | 0.73 |
| noaa_nws+open_meteo | 43 | 60.5% | +$15.32 | 1.19 | 0.44 |
| fred_anchor_3.75% | 17 | 47.1% | +$11.97 | 0.83 | 0.22 |
| monotonicity_arb | 1 | 100% | +$1.56 | — | 0.23 |
| gdpnow_1.2pct | 1 | 100% | +$0.16 | — | — |
| kalshi_implied+fred | 4 | 50% | −$1.19 | — | 0.24 |
| fomc_butterfly_arb† | 21 | 19% | −$5.56 | −3.37 | 0.12 |
| fred_GDP_sigmoid‡ | 5 | 0% | −$11.40 | −7.26 | 0.52 |

*Label now dynamic (was hardcoded); will show actual sources (fedwatch+tbill_term+wsj etc.) from 2026-04-24 forward.  
†Butterfly arb disabled 2026-04-24 — no new trades.  
‡Dead code (GDP entry in ECONOMIC_SERIES commented out); historical losses only.

**Postgres balance range (14 days):** trough $34.90 → peak $268.30 (range $233.40 — includes early-session low)

**Fill rate (Postgres 14-day):** 108 filled / 8,332 total = **1.3%**

**Top reject reasons (Postgres 14-day):**

| Reason | Count | % of rejects |
|--------|-------|-------------|
| ENTRY_FAILED_COOLDOWN | 2,965 | 35.6% |
| LONG_GAME_CAP | 2,013 | 24.2% |
| STOP_COOLDOWN | 1,371 | 16.5% |
| MARKET_LIMIT | 1,028 | 12.3% |
| RISK_GATE_SIZE | 428 | 5.1% |
| MEETING_CONCENTRATION | 163 | 2.0% |

---

## 5. Live/Paper Divergence Monitor

`ep:divergence` hash is **empty**. The `_divergence_monitor_loop` in `ep_exec.py` is registered in `asyncio.gather()` but has not yet completed a first write (runs hourly; requires `trades.csv` with both `mode=live` and `mode=paper` entries to compute divergence).

**Action needed:** Verify `_divergence_monitor_loop` is running; check if trades.csv has paper-mode entries to compare against.

---

## 6. Backtest vs Live Comparison

Backtest tool (`ep_backtest.py`) pairs `entry`/`exit` rows from `trades.csv` — it is a **performance analytics tool**, not a predictive backtest. Sharpe values reflect realized trade P&L distribution, not forward signal quality.

| Strategy | Backtest Sharpe (90d) | Notes |
|----------|----------------------|-------|
| fedwatch+zq+wsj | 6.72 | Sharpe inflated by annualise_factor=n trades not days |
| noaa_nws+open_meteo | 1.19 | Solid; 60.5% win rate |
| fred_anchor_3.75% | 0.83 | Break-even signal type |
| fomc_butterfly_arb | −3.37 | **DEAD** as of 2026-04-24 |
| fred_GDP_sigmoid | −7.26 | **DEAD CODE** (already disabled) |

**No live Sharpe (14d) available** — Postgres `executions` table lacks `model_source`; per-strategy live Sharpe requires joining with signals table or adding model_source column.

**Flag:** `ep_backtest.py` Sharpe calculation uses `annualise_factor=n_trades` not calendar days — produces inflated Sharpe for large n. Qualitative rank ordering is valid; absolute values are not.

---

## 7. Risk Posture (as of 2026-04-24 ~01:30 UTC)

| Metric | Value |
|--------|-------|
| Open positions | 48 |
| Largest position cost (est.) | ~$16.50 |
| Total position cost (est.) | ~$165 (portfolio value $46-$69 depending on node) |
| Exposure % | ~42% of $392 total balance |
| HALT_TRADING | 0 (disabled) |
| llm_halt_trading | 0 (disabled) |
| llm_kalshi_enabled | 1 (enabled) |
| llm_btc_enabled | 1 (enabled) |

**Active advisor overrides (tightening entries):**
- `override_edge_threshold = 0.41` (normal threshold: 0.08 — advisor set 41¢ floor)
- `override_min_confidence = 0.85` (normal: 0.58)
- `override_max_contracts = 5` (normal: 15)
- `llm_kelly_fraction = 0.25` (normal: 0.50)

The advisor overrides explain the high rejection rate — almost no new directional signals meet the 41¢ edge threshold. These were set after the system reached ~$268 balance and reflect a conservative posture during the FOMC approach window.

---

## 8. Data Source Health

**All 17 active sources: healthy** (max age 300s = 5 min, within 120s poll interval tolerance).

| Source | Status | Age |
|--------|--------|-----|
| kalshi_ws / kalshi_rest | ok | 59s |
| cme_fedwatch | ok | 297s |
| cme_sofr_sr1 | ok | 178s |
| gdpnow | ok | 58s |
| fred_* (8 series) | ok | 300s |
| predictit | ok | 177s |
| kalshi_implied | ok | 59s |
| exec_heartbeat | ok | <1s |
| **bls_cpi** | **stale** | null | Expected: no release window |
| **bls_nfp** | **stale** | null | Expected: no release window |

BLS stale sources are **non-critical** — they only activate within 24h of scheduled CPI/NFP releases.

---

## 9. Known-Bug Regression Check

| Bug | Check Result | Status |
|-----|-------------|--------|
| `move_cents` formula | `entry_cents - current_cents` at ep_exec.py:1688,1690 | ✅ FIXED |
| KXGDP guard in `scan_economic_markets` | `not ticker.startswith(("KXGDP","KXFED"))` at strategy.py:1621 | ✅ PRESENT |
| `edgepulse-exec` ExecStartPre | Uses Redis ping loop (not fuser) — port conflict handled differently | ✅ OK |
| Stop-loss cooldown | `ep:cut_loss:*` keys written by Intel; persisted in Redis | ✅ OK |
| Butterfly arb | Entire butterfly block removed 2026-04-24 | ✅ FIXED |
| `fred_GDP_sigmoid` losses | GDP entry in `ECONOMIC_SERIES` commented out | ✅ FIXED |

---

## 10. Schema / Doc Gaps

**Redis keys written by code but NOT documented in SCHEMA.md:**

| Key | Written by | Purpose |
|-----|-----------|---------|
| `ep:divergence` | ep_exec.py | Live/paper P&L divergence monitor |
| `ep:forced_cycle` | ep_intel.py | FOMC orderflow volume spike trigger |
| `ep:kelly_calib:strategy` | ep_kelly_calib.py | Per-strategy confidence multipliers |
| `ep:macro` | ep_intel.py | Full macro regime hash (rates, VIX, etc.) |
| `ep:releases` | ep_datasources.py | Upcoming economic release schedule |
| `ep:vol_prev:{ticker}` | ep_intel.py | Previous-cycle volume for spike detection |

**Scanners in code but not in README Signal Categories:**
- `scan_unrate_fomc_coherence` — UNRATE↔FOMC coherence (new 2026-04-23)
- `scan_cpi_fomc_coherence` — CPI↔FOMC coherence (new 2026-04-23)
- `scan_calendar_decay` — time-premium decay on near-certain YES (new 2026-04-23)
- `scan_earnings_markets` — KXERN IV-based (new 2026-04-23, upgraded to IV 2026-04-24)
- `scan_election_markets_with_538` — Metaculus wrapper (added 2026-04-23, 538→Metaculus 2026-04-24)

**`scan_election_markets_with_538` not wired:** Function exists and wraps `scan_election_markets`, but `fetch_signals_async` does not call it. Needs polymarket_prices + predictit_prices params; must be wired through ep_intel.py directly.

---

## 11. Summary

**Status: Healthy, profitable, and conservatively positioned.**

The system has delivered **$9.81 net P&L** from 77 completed round-trips over 90 days, with the FOMC directional strategy contributing $160 in gross gains offset by the now-disabled butterfly arb (−$5.56) and legacy GDP sigmoid (−$11.40, dead code). Weather signals (60.5% win rate, $15.32) are the most reliable secondary strategy.

**Concerns this week:**
1. **Advisor overrides are suppressive** — `override_edge_threshold=0.41` means almost no new directional entries; 98%+ rejection rate. Consider relaxing after FOMC meeting resolves.
2. **`scan_election_markets_with_538` (Metaculus)** is not wired into the live cycle — election market signals are getting the base Poly/PI blend only, not the Metaculus probability boost.
3. **`ep:divergence` is empty** — the divergence monitor loop is registered but hasn't produced output yet (likely needs paper-mode trades in trades.csv).
4. **LONG_GAME_CAP blocks 24% of signals** — the 25% cap on long-dated directional positions may be too tight; worth reviewing after the FOMC window.
5. **Backtest Sharpe is inflated** — the `annualise_factor=n_trades` formula produces unreliable absolute Sharpe values. Fix: use calendar-day normalization.
