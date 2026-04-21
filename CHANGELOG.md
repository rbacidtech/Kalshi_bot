# EdgePulse — Changelog

All notable changes to the EdgePulse distributed trading system are documented here.

---

## [1.7.0] — 2026-04-21  Fee-adjusted Kelly, per-category Kelly, vol-adjusted sizing, advisor nginx fix

### Fee-adjusted Kelly (kalshi_bot/risk.py)
- **14¢ round-trip fee subtracted before win/loss classification** — Kelly was previously optimistic because `pnl_cents = (exit - entry) * contracts` ignores the 7¢ entry + 7¢ exit fee. Now: `fee_adjusted_pnl = pnl_cents - 2 * fee_cents * contracts`; a trade is a "win" only if the fee-adjusted P&L is positive. Prevents Kelly from sizing up on marginal trades whose gross P&L is positive but net P&L is negative.

### Per-category Kelly (kalshi_bot/risk.py)
- **`_kelly_bucket(model_source)` static method** — maps model_source to one of four buckets: `arb` (`_arb` suffix), `coherence` (`"coherence"` substring), `economic` (`fred_` or `gdp`), `directional` (everything else).
- **`_kelly_by_category: dict` field** on `RiskManager` — stores `{bucket: half_kelly_fraction}` from each daily calibration. Populated alongside `_kelly_cached` (global).
- **`calibrate_kelly()` computes per-bucket fractions** — splits the terminal-trade pool by bucket, runs the same fee-adjusted half-Kelly formula per bucket; falls back to global Kelly if a bucket has < 10 qualifying trades.
- **`size()` looks up category-specific Kelly** — `base_kelly = _kelly_by_category.get(bucket, _kelly_cached)`. Arb signals (historically near 100% win rate on fee-adjusted basis) no longer under-size relative to the global 72% win rate. Economic signals (lower win rate due to surprise uncertainty) no longer over-size.
- **`size()` new params**: `model_source: str = ""` (bucket lookup), `vol_multiplier: float = 1.0` (see below). `effective_kelly = base_kelly * confidence * max(0.1, vol_multiplier)`.

### Volatility-adjusted sizing (ep_exec.py, ep_risk.py, ep_econ_release.py)
- **Pre-release window** (0–168h before print): `vol_multiplier = 0.70` — size economic-bucket signals down 30% in the week before CPI/GDP when uncertainty is highest.
- **Post-release window** (0–48h after print): `vol_multiplier = 1.40` — size up 40% in the 48h after a confirmed print when uncertainty is resolved and the directional edge is strongest.
- **`ep_econ_release.py`**: `_last_release_ts` attribute tracks ISO timestamp of last fired release (set in `_react()` regardless of surprise size); included as `last_release_ts` in `ep:econ_release:status` Redis key. TTL bumped 2h→24h so `last_release_ts` survives service restarts within the post-release window.
- **`ep_exec.py`**: reads `ep:econ_release:status` before Kelly sizing; computes `_vol_multiplier` from `next_time_utc` (pre-release) and `last_release_ts` (post-release); passes to `risk_engine.size(sig, balance_cents, vol_multiplier=_vol_multiplier)`.
- **`ep_risk.py`**: `UnifiedRiskEngine.size()` accepts `vol_multiplier=1.0` and passes it to `RiskManager.size()`.

### Advisor tab nginx fix (nginx config)
- **`advisor` missing from nginx proxy pattern** — `/advisor/status` and `/advisor/alerts` requests were falling through the API proxy rule and being served `index.html` by the SPA catch-all. Added `advisor` to the `location ~ ^/(auth|keys|...|advisor|...)` regex in `/etc/nginx/sites-available/edgepulse`. Reloaded nginx.

---

## [1.6.0] — 2026-04-21  Signal priority, partial-fill exposure, exit TIF escalation

### Signal queue prioritization (ep_schema.py, ep_intel.py, ep_bus.py)
- **`priority` field added to `SignalMessage`** — `int`, default 3. Intel sets 1 for arb (`category=="arb"`, `arb_legs` set, or `model_source.endswith("_arb")`), 2 for coherence (`"coherence" in model_source`), 3 for directional.
- **Intel publishes in priority order** — `new_signals.sort(key=_signal_priority)` before the publish loop; arb signals land first in the stream every cycle.
- **Exec sorts each consumed batch** — `consume_signals` buffers each `count=10` batch, sorts by `priority`, then yields in order. Within a 120s window, arb signals are always processed before directional ones regardless of stream insertion order.

### Partial fill exposure (ep_positions.py)
- **`contracts_filled: 0` initialized in `PositionStore.open()`** — field was previously absent until `_fill_poll_loop` first wrote it; now exists from creation for consistent access.
- **`total_exposure_cents()` uses `contracts_filled`** — `p.get("contracts_filled") or p.get("contracts", 1)`. During the resting-order window (9 of 15 filled), exposure is now computed on the filled quantity, not the requested size. Prevents over-stating risk on large partially-filled FOMC orders.

### Exit order TIF escalation (kalshi_bot/executor.py, ep_exec.py)
- **`executor._exit_position` returns `order_id`** (str) — `"paper"` for simulated, actual `order_id` for live, `""` on API failure. Paper exits still delete from `_positions` immediately; live exits set `_positions[ticker]["pending_exit"] = True` to block re-entry.
- **Exit checker writes `pending_exit` state to Redis** — on a successful live exit, stores `exit_order_id`, `exit_order_placed_at`, `exit_offer_cents`, `exit_reason`, `exit_widen_count=0` instead of immediately closing the position. Subsequent exit-checker cycles skip `pending_exit=True` positions.
- **TIF escalation in `_fill_poll_loop`** — scans for `pending_exit=True` positions each 90s poll cycle:
  - Order filled → close position and clear executor entry
  - Order resting > `EXIT_TIF_STEP_MINUTES` (default 30 min) and `exit_widen_count < EXIT_TIF_MAX_STEPS` (default 3) → cancel resting limit, place new limit at `offer − EXIT_TIF_WIDEN_CENTS` (default 2¢), update state
  - After 3 steps (90 min total), holds at final price; logs warning
  - Order canceled externally → close position
- **Arb-group sibling exits** use same pattern; `_sib_ok` now based on return value instead of `not in executor._positions`.
- **Resolution-driven exits** call `executor._positions.pop()` immediately (market is over, TIF pointless).
- **New env vars**: `EXIT_TIF_STEP_MINUTES=30`, `EXIT_TIF_WIDEN_CENTS=2`, `EXIT_TIF_MAX_STEPS=3`

---

## [1.5.0] — 2026-04-21  Arb/coherence YES gate exemption

### YES price gate: arb and coherence signals exempt (kalshi_bot/strategy.py, ep_intel.py)
- **`_PRICE_GATE_EXEMPT_SOURCES` frozenset** added to `strategy.py` — explicit manifest of model_source values whose edge derives from relative contract mispricing, not absolute price level (`fomc_butterfly_arb`, `monotonicity_arb`, `calendar_spread_arb`, `gdp_fomc_coherence`, `cross_series_coherence`). These bypass `MIN_YES_ENTRY_PRICE` even if future refactors restructure the gate.
- **`scan_fomc_directional()` gate** extended: condition now includes `and model_src not in _PRICE_GATE_EXEMPT_SOURCES`. Suppression log now prints `model_source` for easier diagnosis.
- **Treasury auction proximity suppression** (`ep_intel.py`) — previously kept only arb signals (`model_source.endswith("_arb")`); now keeps coherence signals too (`"coherence" in model_source`). `gdp_fomc_coherence` signals for deep OTM KXFED strikes (e.g., KXFED-26DEC-T3.75) were being dropped alongside directional signals during 10Y/30Y auction windows.
- **FOMC announcement blackout** — same fix; coherence signals now survive the ±2h blackout window around the 2pm ET announcement.

---

## [1.4.0] — 2026-04-21  Arb rollback hardening, signal TTL, resolution-DB calibration

### Arb rollback hardening (kalshi_bot/executor.py, ep_exec.py, tests/)
- **`ArbRollbackFailed` exception** — raised by `execute_arb_legs()` when a leg fails *and* at least one earlier cancel also fails. Carries `unrecovered: list[(ticker, side, order_id)]` attribute.
- **`_arb_cancel_placed()` return type** changed from `None` to `list` of failed-cancel tuples. Never raises.
- **ep_exec.py orphan recovery** — catches `ArbRollbackFailed`; writes each unrecovered leg to `ep:positions` as `model_source="arb_unrecovered"` so `_exit_checker` can close them; fires critical alert to `ep:alerts` stream and Telegram. Plain `RuntimeError` (clean rollback) still handled as before.
- **`tests/test_arb_rollback.py`** — 6 scenarios: all-legs-succeed, clean-rollback, partial-cancel-failure, paper-mode, `_arb_cancel_placed` return value. All pass.

### Signal TTL bump (ep_config.py, ep_exec.py)
- **`EP_SIGNAL_TTL_MS` default: 30 s → 60 s** — 13-gate chain + Kelly compute + REST call was causing legitimate signals to expire under Redis latency spikes.
- **EXPIRED signals now log at INFO** (was DEBUG) — visible in normal log output; easier to monitor rate.
- **Near-expiry stop suppression reads `ep:config` override** — `kalshi_near_expiry_no_stop_days` key in Redis overrides the env var at runtime without restart.

### Resolution-DB threshold calibration (ep_resolution_db.py, ep_advisor.py, kalshi_bot/strategy.py, ep_intel.py)
- **`compute_yes_entry_price_gate()`** — bins KXFED YES trades by 5¢ entry price buckets; finds lowest bucket with positive EV; returns calibrated threshold (fraction) or default 0.60 if <20 qualifying trades.
- **`compute_near_expiry_stop_days()`** — compares short-hold vs long-hold loss rate across KXFED mid-price (30–70¢) trades; finds the hold-day threshold where noise-driven loss rate drops ≥10pp; returns calibrated days or default 7 if insufficient data.
- **ep_advisor.py** calls both functions each cycle; auto-writes calibrated values to `ep:config` as `override_min_yes_entry_price` and `kalshi_near_expiry_no_stop_days` when data is sufficient; includes calibration results in Claude's context.
- **`scan_fomc_directional()` + `fetch_signals_async()`** accept `min_yes_entry_price` parameter override — caller-supplied value (from Redis config) wins over env/config default.
- **ep_intel.py** reads `override_min_yes_entry_price` from `ep:config` each cycle and passes it to `fetch_signals_async`.

---

## [1.3.0] — 2026-04-20  Performance overhaul: YES filter, Kelly fix, near-certain hold, wider stops, arb execution

### YES signal suppression (kalshi_bot/strategy.py, kalshi_bot/config.py)
- **`MIN_YES_ENTRY_PRICE=0.60`** gate in `scan_fomc_directional()` — KXFED YES signals with `market_price < 0.60` are suppressed. Data showed YES entries below 60¢ have 11–13% win rate (avg −55¢ to −116¢/trade); above 60¢ are profitable. Configurable via `KALSHI_MIN_YES_ENTRY_PRICE`.

### Kelly calibration fix (kalshi_bot/risk.py)
- `calibrate_kelly()` now filters to **terminal-only trades** (exit at 0¢ or 100¢, i.e. market resolution) before computing Kelly parameters. Previously used all exits including stop-loss/pre-expiry, giving win_rate=3.4% and `full_kelly=-0.76` (impossible — bet nothing). Terminal win rate = 71.8%, corrected `full_kelly≈+0.44`.
- Falls back to full population when fewer than `MIN_KELLY_TRADES` (10) terminal trades exist.
- `KALSHI_MIN_KELLY_TRADES` env var controls minimum terminal-trade threshold.

### Near-certain hold logic (ep_exec.py)
- **`KALSHI_NEAR_CERTAIN_THRESHOLD_CENTS=8`** — positions where YES price ≤ 8¢ or ≥ 92¢ (near-certainty) are exempted from pre-expiry forced exits. These contracts were being exited 24–48h early at a discount instead of holding to auto-resolution at 0¢ or 100¢.
- Near-certain skip guard runs BEFORE the pre-expiry block in `_exit_checker`.

### Near-expiry stop suppression (ep_exec.py)
- **`KALSHI_NEAR_EXPIRY_NO_STOP_DAYS=7`** — stop-loss is suppressed within 7 days of `close_time`. Contracts this close to expiry should resolve naturally; stop-losses here are noise-driven and cut winners.
- Affects both stop-loss and trailing stop.

### Wider exits and larger sizing (.env, ep_exec.py)
- `KALSHI_TAKE_PROFIT_CENTS`: 20 → **40** — FOMC contracts need room to move; 20¢ TP was exiting before contracts reached their fair value floor (0¢).
- `KALSHI_STOP_LOSS_CENTS`: 15 → **30** — 15¢ was within the spread noise band for high-value contracts; stops were firing on bid-ask jitter.
- `KALSHI_MAX_CONTRACTS`: 5 → **15** — quarter-Kelly sizing with 71.8% terminal win rate warrants larger positions.
- `KALSHI_MAX_MARKET_EXPOSURE`: 10% → **20%** — Kelly analysis warrants higher per-position allocation.

### Butterfly arb multi-leg execution (ep_exec.py, kalshi_bot/executor.py, ep_schema.py, ep_adapters.py)
- `scan_fomc_arb()` now populates `arb_legs` list on butterfly `Signal` objects with 3-leg structure (buy leg A, sell leg B, buy leg C).
- `ep_exec.py` dispatches arb signals to `executor.execute_arb_legs()` — all legs placed atomically; partial fill triggers best-effort cancel of already-placed legs.
- `arb_legs: Optional[List[dict]]` field added to `SignalMessage` dataclass.
- `ep_adapters.py` propagates `arb_legs` from `Signal` through to `SignalMessage`.

### FOMC model improvements (kalshi_bot/models/fomc.py)
- **Tiered staleness penalty** replaces flat 0.80× haircut: <30min → 1.0×, 30min–2h → 0.80×, 2–6h → 0.50×, >6h → 0.0 (signal blocked entirely).
- **`data_quality` field** on `MeetingProbs` — `"fallback_only"` when running on FRED static only with no Kalshi-implied and no ZQ futures data.
- **`FALLBACK_ONLY_EDGE_THRESHOLD=0.25`** — fallback-only signals require 25¢ edge instead of 10¢.
- **Late-money spike penalty softened**: 0.70× → 0.90× multiplier (was over-penalizing valid FOMC signals).
- Probability clamp tightened: `max(0.05, min(0.95, raw))` (was 0.01/0.99).

---

## [1.2.0] — 2026-04-19  Signal quality: FOMC butterfly, cross-series coherence, calendar spread, ADP, VIX gate, Polymarket fixes

### FOMC butterfly spread arb (kalshi_bot/strategy.py)
- Detects convexity violations across equal-spaced KXFED strikes: `P(A) + P(C) - 2*P(B) < -0.04`; generates signals on the overpriced middle strike with `model_source="fomc_butterfly_arb"`, `confidence=0.70`
- Violations sorted by edge; up to 26 opportunities detected per cycle

### Cross-series GDP-FOMC coherence (kalshi_bot/strategy.py)
- `scan_cross_series_coherence()`: when GDPNow < 1.5%, computes `implied_cut_prob = (2.0 - gdpnow) * 0.3 + 0.40` and signals YES on KXFED T3.75/T4.00 strikes below that probability
- **45-day minimum filter** — skips meetings expiring within 45 days; prevents spurious April signals where rate cannot physically reach target in time
- Generating `KXFED-26DEC-T3.75` (11¢) and `KXFED-26DEC-T4.00` (14¢) YES signals with GDPNow=1.31%

### Calendar spread rate-path arb (kalshi_bot/strategy.py)
- `scan_rate_path_value()`: for same strike across adjacent FOMC meetings, emits NO on later meeting if `later_yes > earlier_yes + 0.10`; `model_source="calendar_spread_arb"`, `confidence=0.65`

### ADP leading indicator for NFP (kalshi_bot/strategy.py)
- ADP (ADPWNUSNERSA from FRED) fetched concurrently with PAYEMS; direction agreement boosts NFP confidence 1.10×, disagreement reduces 0.85×
- Z-score surprise model: `(most_recent - mean_last_6) / std_last_6`; |z|>1.5 → +0.05 confidence, |z|>2.5 → +0.10

### VIX/MOVE confidence gating (ep_intel.py)
- FOMC directional signals gated by volatility: VIX≥35 → 0.80×, VIX≥25 → 0.90×; MOVE>120 → −0.05 (floored at 0.10)
- Exempts signals with `model_source` ending in `_arb`

### Polymarket fixes (ep_polymarket.py)
- **Pagination**: `_GAMMA_PAGE_SIZE=500`, `_GAMMA_MAX_PAGES=10` — fetches up to 4,970 active markets (was limited to first page)
- **outcomePrices decode**: `json.loads()` before indexing — was silently producing 0 prices (root bug)
- **DIVERGENCE_THRESHOLD** lowered 0.04 → 0.02
- **Degenerate price filter**: skips matches where `poly_yes < 0.02 or poly_yes > 0.98` (stale/wrong matches)
- **Minimum volume filter**: rejects matches where `best.volume_24h < 1000` — prevents structural mismatches (range markets vs threshold markets for GDP)

### Dashboard CSS fix (dashboard/)
- `postcss.config.js` created (was missing) — Vite was never running PostCSS, so Tailwind directives were passed raw to browsers
- `tailwindcss@3` and `autoprefixer` added as devDependencies; rebuilt CSS 2kB → 39.27kB

---

## [1.1.0] — 2026-04-19  Cut-loss mechanism, exit order fix, weather scanner, bug fixes, deploy tooling

### Cut-loss mechanism (ep_intel.py + ep_exec.py)
- **`ep:cut_loss:{ticker}` Redis key** — Intel writes this when a held GDP position's fundamental signal has reversed beyond the cut-loss threshold (0.75pp gap for both YES and NO sides, within 14 days of expiry). Replaces the old auto-tombstone which used a 2pp threshold with a ≤7-day window — too conservative to catch the current KXGDP-26APR30-T2.5 YES position.
- **Cut-loss consumer in `_exit_checker`** — Exec scans `ep:cut_loss:*` every 60 s. For fill-confirmed positions it adds them to `_cutloss_tickers` and triggers a proper sell order in the main exit loop (`exit_reason = "cut_loss_intel"`). For resting (unconfirmed) orders it calls `cancel_and_tombstone`.
- **Per-cycle GDP check covers NO positions** — previously only warned on YES positions. Now computes directional gap for both sides and acts on whichever is offside.
- **Startup GDP check extended** — same 0.75pp cut-loss write at service startup so the first cycle acts immediately rather than waiting for the first per-cycle check.

### Exit order fix (kalshi_bot/executor.py)
- **Changed from `"action": "buy", "side": "<opposite>"` to `"action": "sell", "type": "limit", "side": "<same>"`** — the previous exit code tried to open a new opposing-side position (required Kalshi balance) rather than selling the contracts already held. Kalshi has no true market orders; all orders require a price field. Exit now uses the correct `yes_price` / `no_price` field based on the held side.
- **Error logging improved** — `HTTPError` now logs `exc.response.text[:300]` so Kalshi's error body is visible in the log instead of just the HTTP status code.

### cancel_and_tombstone bug fix (ep_exec.py)
- **Was passing `bus` (RedisBus) where `executor.client` (KalshiClient) is required** — the function calls `client._request("DELETE", ...)` which would AttributeError on a RedisBus. Worked silently before because all positions had `order_id="paper"` (condition was skipped). Fixed to pass `executor.client` in both the tombstone consumer and the new cut-loss consumer.

### Dashboard: Controls page (dashboard/src/pages/ControlsPage.tsx)
- Full tab redesign: Status · Strategies · Risk · AI Advisor
- Range sliders with CSS gradient fill for all risk parameters
- Status tab reads `ep:health` and `ep:balance` (correct keys)
- AI Advisor tab calls `claude-haiku-4-5-20251001` via `/controls/ai-suggest`
- Colour-coded callouts: live overrides (edge/contracts/confidence) vs restart-required (kelly/exposure/drawdown/poll)

### Dashboard: style pass (all pages)
- Removed all gradient card backgrounds; replaced with flat `bg-surface-1` + 3px top-border accent + matching box-shadow
- `WinRateRing` rewritten to SVG `strokeDasharray` — eliminates dark artifact at low win rates
- Login/Register pages: dark `#0a0f1e` background, no white card
- Keys, Subscription, Admin pages: per-entity accent colour on top borders
- Layout route titles: added `/performance`, `/controls`, `/keys`, `/subscription`

### API: controls router (api/routers/controls.py)
- **`get_config`** now layers three sources: env defaults → `ep:bot:config` UI state → `ep:config` hash live overrides
- **`patch_config`** writes to both `ep:bot:config` (full JSON for UI) and `ep:config` hash (three live-override fields the bot reads each cycle)
- **`get_status`** fixed: reads `ep:health` (hgetall) and `ep:balance` (hgetall) with `_ts_us_to_iso` timestamp conversion

### Intel: pnl_snapshots fix (ep_intel.py)
- `_write_pnl_snapshot` was calling `hgetall("ep:performance")` — WRONGTYPE error because `ep:performance` is a STRING key. Fixed to `r.get("ep:performance")` + JSON parse. Silently aborting before `write_snapshot()` was the reason `pnl_snapshots` table had 0 rows.

### Weather scanner (kalshi_bot/strategy.py)
- **Dual-source model** — Open-Meteo (primary) + NOAA NWS daily (secondary) for high/low temp and precipitation markets. Sigma grows with forecast horizon (2.5°F day 0–1 → 5.5°F day 4+). Source agreement widens sigma to model disagreement.
- **Threshold from `floor_strike`** — market object field used directly instead of parsing the title (regex was failing on `>61°` without the `F` suffix).
- **`strike_type` direction** — `"less"` markets (e.g. `KXHIGHCHI-26APR19-B48.5`) now correctly compute `1 − P(above)`.
- **Price filter fixed** — `price <= 0.01` → `price < 0.01` so 1¢ markets (valid thin-book prices) are no longer filtered.
- **Same-day market filter** — `days_ahead == 0` markets skipped; they close within 24h and would immediately trigger the pre_expiry exit logic.
- **`WEATHER_SERIES`** — NYC, LA, Chicago, DC high/low + NYC rain; NWS grid coordinates per city.

### Exit / state fixes (ep_exec.py + kalshi_bot/executor.py)
- **Tombstone guard** — `contracts == 0` check added at the top of the exit loop; tombstones (written by `cancel_and_tombstone`) no longer trigger `sell count=0` API calls that returned HTTP 400.
- **ResolutionDB schema migration** — `trade_outcomes` table had stale schema missing `series`, `entry_cents`, `exit_cents` columns. `ep_resolution_db.py` now applies `ALTER TABLE ADD COLUMN` migrations on init for all three.

### Exit count=0 fix (ep_exec.py + kalshi_bot/executor.py)
- **Root cause**: `_exit_position(ticker, pos, ...)` read `pos["contracts"]` directly (always 0 for tombstones) instead of the fill-poll-derived `contracts_filled` count that ep_exec.py had already computed. Result: every exit after a cancel_and_tombstone sent `"count": 0` to Kalshi → HTTP 400 forever.
- **Fix**: exit call now passes `{**pos, "contracts": contracts}` where `contracts = pos.get("contracts_filled") or pos.get("contracts", 1)`. Same fix applied to resolution-driven exit path.
- **KXGDP-26APR30-T2.5 zombie cleared**: position with contracts=0/entry_cents=0 was looping exit failures every 600s since 07:44 UTC. Manually deleted from Redis. Underlying 3-contract YES position on Kalshi will resolve NO on April 30 (GDPNow=1.31% vs T2.5 threshold).

### Operations
- **`deploy.sh`** — new script: `rsync` ep_*.py + kalshi_bot/ to quantvps with checksum verification, then restarts both services. Accepts `--intel`, `--exec`, `--sync` flags.

---

## [1.0.0] — 2026-04-16  Production hardening: security, correctness, live trading

### Security hardening
- **Redis `requirepass`** added — all connections now authenticate with 64-character token
- **FLUSHALL / FLUSHDB disabled** via `rename-command ""` in docker-compose — eliminates cryptominer attack vector (root cause of nightly position wipe: attacker at 34.70.205.211 called FLUSHALL every ~25 min via unauthenticated Redis)
- **Redis `activedefrag yes`** — continuous background defragmentation; memory fragmentation ratio dropped from 5.85 → 1.20 after BGREWRITEAOF
- **UFW enabled on both nodes** — Intel: 6379 only from QuantVPS IP; Exec: 22 open, 9092 only from Intel IP; default deny incoming on all other ports
- **fail2ban** — installed on both nodes; maxretry=3, bantime=1h; immediately banned 10+ SSH brute-force attackers on Intel
- **SSH hardening on Exec** — `PasswordAuthentication no`; only Intel's ed25519 key accepted
- **Redis AOF rewritten** — `BGREWRITEAOF` removed malicious FLUSHALL + cron injection commands from persistent AOF log
- **Grafana password** rotated from default "changeme"
- **Kalshi client env** updated to `REDIS_URL` with password on both nodes

### Correctness fixes (execution pipeline)
- **fill_poll partial-cancel bug** — orders with `status=canceled AND fill_count>0` previously looped forever as "PARTIAL FILL"; now finalized immediately with actual filled quantity
- **fill_poll executor sync** — after `positions.update_fields()` in fill_poll, now mirrors update into `executor._positions` to prevent state divergence handler from restoring stale `fill_confirmed=False`
- **Race condition in exit path** — `executor._positions.pop()` now runs before `positions.close()` so exit_checker cannot fire between the two operations
- **Right-tail truncation guard** — NO signals for strikes > `current_rate + 0.50` suppressed; HIKE_50 is the model ceiling and edge at T4.50/T4.75 was a probability floor artifact, not real edge
- **NO cost in Kelly sizing** — `price_cents = 100 - market_price_cents` for NO side; was incorrectly using YES price, causing over-sizing
- **NO cost in approve() gate** — `order_cost = (100 - entry_cents) × contracts` for NO side; was using YES price, causing under-counting in exposure checks
- **NO cost in per-series/category limits** — `sig_cost` and `t_cost` now use `(100 - entry)` for NO positions; was double-counting exposure as if buying YES
- **NO cost passed to UnifiedRiskEngine** — `side=sig.side` now forwarded to `_kalshi.approve()` in `ep_risk.py`
- **Retry loop cooldowns** — `BALANCE_UNKNOWN`, `RISK_GATE_SIZE` (10 min), `UNKNOWN_ASSET_CLASS` now set `_entry_failed_cooldown` to prevent hot retry loops on transient failures
- **Startup orphan reconciliation** — `_reconcile_orphan_orders()` on startup: fetches resting Kalshi orders, restores any missing from Redis (prevents positions disappearing after Redis wipe)

### Signal quality
- **Edge at ask-price** — published edge now adjusted by half-spread before Intel publishes; prevents trading signals that only look good at mid
- **Spread-to-edge filter** — signals where `spread > edge` (guaranteed negative EV) suppressed at Intel
- **GDP YES signal suppression** — KXGDP YES signals skipped when `GDPNow < (strike - 0.50)`
- **GDP startup risk check** — Intel warns on startup if any KXGDP YES position has GDPNow materially below strike
- **KXGDP excluded from economic scanner** — GDP markets were being double-processed; now handled only by the dedicated GDP scanner

### Infrastructure
- **Fee-aware P&L logging** — entry/exit reports now subtract `FEE_CENTS × contracts` so reported P&L is net of exchange fees
- **Consumer group recovery** — ep:executions consumer now starts from `id="0"` (replays from stream head) instead of `id="$"` (skip) on group creation; prevents losing execution reports after Redis restart
- **Consumer group NOGROUP handler** — two-pass mkstream strategy with INFO logging on creation vs recovery
- **PYTHONUNBUFFERED=1** in both systemd service files — log output flushes immediately
- **Kalshi API circuit breaker** — halts exec after 5 consecutive API errors; prevents runaway retry storms
- **Daily risk reset** — `set_balance()` resets `_start_balance` and `_halted` at UTC midnight

### Monitoring
- **Exec peer liveness check** — Intel warns if exec HEARTBEAT is > 120s old
- **CME FedWatch OAuth2** — confirmed working with `auth.cmegroup.com/as/token.oauth2` endpoint; confidence 0.92 with dual-source (Kalshi-implied + FRED static fallback)
- **ep:prices backfill** — positions below current edge threshold are backfilled with last-known price snapshot so exit_checker has data for all held positions

---

## [0.9.0] — 2026-04-15  Structural stabilization

### Changes
- **Flat directory structure** — `EdgePulse-Trader/` subdirectory removed; all source files live at repo root
- **systemd service** — `edgepulse-exec.service` enabled as managed unit on QuantVPS; `ExecStartPre` kills stale `:9092` processes on startup
- **Single-leg arb fix** — both legs of a Kalshi arb signal now execute atomically in `_process_signal`
- **FOMC model_src label** — displays `kalshi_implied+fred` accurately when Kalshi prices are the primary source
- **close_time backfill** — existing positions with null `close_time` field are backfilled on exec startup

---

## [0.8.0] — 2026-04-12  FOMC model v2 — CME FedWatch fusion

### Changes
- **CME FedWatch primary source** — FedWatch probabilities via OAuth2 token exchange now primary FOMC model input
- **FRED FF1/FF2/FF3 fallback** — 30-day fed funds futures as secondary fallback when CME unavailable
- **FRED DFEDTARU anchor** — live effective fed funds rate fetched daily; replaces static `CURRENT_FED_RATE` env var
- **Confidence scoring** — signal confidence 0.95 (CME primary) → 0.92 (Kalshi-implied) → 0.75 (FRED static)
- **GDP scanner** — KXGDP markets added to signal pipeline with GDPNow integration
- **Kalshi-implied fallback** — if all external sources unavailable, derives probability distribution from Kalshi YES prices directly

---

## [0.7.0] — 2026-04-08  Performance audit + data source health registry

### Changes
- **`ep_health.py`** — data source health registry; tracks last-success timestamp and error counts per source
- **Async order book fetching** — `client.get_many()` with `httpx.AsyncClient` and semaphore-limited concurrency; full scan time from O(n) → O(1) wall-clock
- **Per-request timeout override** — `per_request_timeout` param in `get_many()` prevents hung connections from blocking asyncio cleanup
- **Exec startup state divergence check** — compares `executor._positions` against Redis on startup; logs and repairs any mismatches
- **Prometheus metrics** — `ep_metrics.py` added; Intel scrape on `:9091`, Exec on `:9092`; Grafana auto-provisioning added

---

## [0.6.0] — 2026-04-05  Distributed architecture — EdgePulse v1

### Changes
- **Two-node split** — Intel (DO NYC3) + Exec (QuantVPS Chicago) communicate via Redis Streams
- **`ep_bus.py`** — `RedisBus` wrapping Redis Streams + Hash I/O; consumer groups with XREADGROUP
- **`ep_schema.py`** — `SignalMessage`, `ExecutionReport`, `PriceSnapshot` dataclasses with JSON round-trip
- **`ep_exec.py`** — Exec main loop: signal consumption, risk gate, Kalshi/Coinbase order placement, exit checker, fill poll
- **`ep_intel.py`** — Intel main loop: 120s scan cycle, price publishing, signal deduplication
- **`ep_risk.py`** — `UnifiedRiskEngine`: Kalshi Kelly + BTC daily loss cap in one gate
- **`ep_positions.py`** — Redis-backed `PositionStore`; `ep:positions` as source of truth
- **`ep_coinbase.py`** — Coinbase Advanced Trade (CDP) client for BTC execution
- **`ep_adapters.py`** — Signal ↔ SignalMessage translation layer
- **`ep_btc.py`** — BTC mean-reversion strategy: RSI-14 + Bollinger Bands + z-score; all three required simultaneously
- **`ep_polymarket.py`** — Polymarket CLOB arb signal source (resting)
- **`ep_behavioral.py`** — Behavioral pattern filters (news-window suppression, post-FOMC cooldown)
- **`ep_telegram.py`** — Telegram alert integration (disabled pending bot token)
- **LLM policy loop** — `llm_agent.py`; Claude reads Redis state every 4-6h and writes JSON policy to `ep:config`
- **`docker-compose.yml`** — Redis 7, Prometheus, Grafana on Intel node

---

## [0.5.0] — 2026-03-20  Strategy v2 — universal Kalshi scanner

### Changes
- **Universal market scanner** — scans all Kalshi markets by category: weather, economic, sports
- **Fee model** — `FEE_CENTS=7` applied to Kelly sizing and edge threshold
- **Stats tracker** (`stats.py`) — per-signal P&L and win-rate tracking
- **FOMC arb** — monotonicity violation scanner across T-level strikes
- **`SCHEMA.md`** — Redis key and message schema documentation

---

## [0.4.0] — 2026-03-05  First live trading session

### Changes
- **Live mode flag** — `KALSHI_PAPER_TRADE=false` enables real order placement
- **Kelly fraction** — 25% Kelly (quarter-Kelly) as default; configurable via `KALSHI_KELLY_FRACTION`
- **Exposure gates** — per-market (5%) and total (30%) caps; daily drawdown halt at 20%
- **`SETUP_CHECKLIST.md`** — step-by-step deployment guide

---

## [0.3.0] — 2026-02-20  Risk management + backtester

### Changes
- **`kalshi_bot/risk.py`** — `RiskManager`: Kelly sizing, spread gate, exposure caps, daily drawdown halt
- **Backtester** (`kalshi_bot/models/backtester.py`) — historical signal replay with P&L attribution
- **FOMC directional v1** — FRED-anchored fair value vs Kalshi market price

---

## [0.2.0] — 2026-02-10  Async client + Kalshi WebSocket

### Changes
- **`kalshi_bot/client.py`** — sync + async (httpx) Kalshi REST client with retry/backoff
- **`kalshi_bot/websocket.py`** — Kalshi WebSocket price feed
- **`dashboard.py`** — Streamlit trading control panel

---

## [0.1.0] — 2026-02-01  Initial single-node Kalshi bot

### Features
- Single-process FOMC prediction market scanner
- RSA-signed Kalshi API authentication
- Paper trading mode
- Basic signal generation from FRED + Kalshi prices
- Logging, retries, configurable thresholds via `.env`
