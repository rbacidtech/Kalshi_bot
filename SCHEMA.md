# EdgePulse-Trader — Redis Message Schema v1

## Design principles

| Principle | Implementation |
|---|---|
| Self-describing | Every message carries all fields needed to act on it — no foreign-key lookups |
| Loss-tolerant | No ordering dependencies; duplicate delivery is safe (idempotent dedup on `signal_id`) |
| TTL-gated | Exec discards any `SIGNAL` older than `ttl_ms` — never acts on stale edges |
| Append-only audit | Streams are never mutated; consumer groups provide exactly-once processing |
| Typed | `msg_type` field on every message; unknown types are silently dropped |

---

## Redis key layout

```
ep:signals                STREAM   Intel → Exec      edge opportunities
ep:executions             STREAM   Exec  → Intel     fill confirmations
ep:positions              HASH     Exec writes       ticker → position JSON
ep:prices                 HASH     Intel writes      ticker → price snapshot JSON
ep:balance                HASH     both write        node_id → balance JSON
ep:health                 HASH     Intel writes      node_id → health JSON
ep:system                 STREAM   both write        lifecycle events
ep:config                 HASH     ops writes        runtime override flags
ep:performance            STRING   Exec+Intel        hourly P&L summary JSON (TTL 25h)
ep:cooldown:{ticker}      STRING   Exec writes       stop-loss cooldown TTL key (30min/2h/24h)
ep:stopcnt:{ticker}       STRING   Exec writes       stop-loss escalation counter (TTL 7 days)
ep:cut_loss:{ticker}      STRING   Intel writes      fundamental cut-loss signal (TTL 300s)
ep:tombstone:{ticker}     STRING   Intel writes      cancel resting order + block re-entry
ep:bot:config             STRING   Dashboard         SaaS UI state JSON
ep:alerts                 STREAM   ep_advisor writes advisor alerts (maxlen 500)
ep:advisor:status         STRING   ep_advisor writes last advisor run snapshot (TTL 2h)
ep:advisor:spread_wide_since STRING ep_advisor      BTC cross-exchange spread timer
ep:econ_release:status    STRING   ep_econ_release  next/last economic release metadata (TTL 24h)
ep:resolutions            HASH     ep_resolution_db  per-series resolution history (last 10)
ep:macro                  HASH     Intel writes      macro indicators snapshot (see below)
ep:releases               HASH     Intel writes      latest BLS release values (set on release day only)
ep:forced_cycle           STRING   Intel writes      "1" = skip next sleep, trigger immediate scan (TTL 120–300s)
ep:vol_prev:{ticker}      STRING   Intel writes      FOMC-day KXFED volume snapshot for spike detection (TTL 600s)
ep:divergence             HASH     Exec writes       edge-capture ratio from last 7-day completed trades (hourly)
ep:kelly_calib:strategy   HASH     ep_kelly_calib    model_source → empirical confidence multiplier (TTL 48h)
```

### Consumer groups

```
ep:signals     →  consumer group "exec-consumers"   (Exec node reads)
ep:executions  →  consumer group "intel-consumers"  (Intel node reads)
```

Both streams are capped at `MAXLEN ~= APPROX` so they never grow unbounded.

---

## Message types

### 1. `SIGNAL` — edge opportunity

Published by Intel to `ep:signals`. Exec must process or discard within `ttl_ms`.

```jsonc
{
  // ── Identity ────────────────────────────────────────────────────────────
  "signal_id":       "a3f7c2d1-...",   // UUIDv4 — deduplicate on this
  "schema_version":  "1",
  "msg_type":        "SIGNAL",
  "source_node":     "intel-do-nyc3",
  "ts_us":           1718000000000000, // microseconds since UNIX epoch (UTC)
  "ttl_ms":          30000,            // Exec MUST discard if now > ts_us + ttl_ms*1000

  // ── Classification ───────────────────────────────────────────────────────
  "asset_class":     "kalshi",         // "kalshi" | "btc_spot" | "cme_btc_basis"
  "strategy":        "fomc_directional",
  // strategy values:
  //   "fomc_directional"   — FRED-anchored fair value vs Kalshi price
  //   "fomc_arb"           — monotonicity violation across T-level contracts
  //   "fomc_weather"       — NOAA NWS temperature/precip model
  //   "fomc_economic"      — FRED macro threshold model
  //   "btc_mr"             — BTC mean-reversion z-score
  //   "cme_btc_basis"      — CME futures vs spot basis (future)
  "category":        "fomc",           // "fomc" | "arb" | "weather" | "mean_reversion" | "basis"

  // ── Market ───────────────────────────────────────────────────────────────
  "ticker":          "KXFED-27APR-T4.25",
  "exchange":        "kalshi",         // "kalshi" | "coinbase" | "bybit" | "cme"
  "side":            "yes",            // "yes" | "no"  (Kalshi)  "buy" | "sell" (BTC)

  // ── Pricing ──────────────────────────────────────────────────────────────
  "market_price":    0.72,    // current mid-price  (0–1 for Kalshi, USD for BTC)
  "fair_value":      0.85,    // model-derived fair value (same units as market_price)
  "edge":            0.13,    // abs(fair_value - market_price)  gross edge
  "fee_adjusted_edge": 0.06,  // edge after estimated exchange fees

  // ── Confidence & sizing ──────────────────────────────────────────────────
  "confidence":      0.88,    // [0, 1]  model confidence
  "suggested_size":  4,       // contracts / units from Kelly sizing on Intel
  "kelly_fraction":  0.25,    // kelly_f used to produce suggested_size
  "priority":        3,       // execution priority within batch: 1=arb 2=coherence 3=directional

  // ── Risk flags ───────────────────────────────────────────────────────────
  // Exec uses these as advisory. Unknown flags are treated as WARNING.
  // Never fail-closed on unknown flags — Intel/Exec may be on different versions.
  "risk_flags": [],
  // Possible values:
  //   "WIDE_SPREAD"       spread_cents > configured max
  //   "LOW_LIQUIDITY"     volume below minimum threshold
  //   "STALE_DATA"        model data > 10 min old (confidence already reduced)
  //   "HIGH_CONFIDENCE"   confidence > 0.90 (positive flag — size up allowed)
  //   "NEAR_EXPIRY"       < 24h to market resolution
  //   "MODEL_DIVERGENCE"  FedWatch and ZQ futures disagree > 4%
  //   "ARB_PARTNER"       this signal has a paired leg (see arb_partner field)

  // ── Market microstructure ────────────────────────────────────────────────
  "spread_cents":    4,       // bid-ask spread in cents (null if unknown)
  "book_depth":      250,     // total quantity at best bid+ask

  // ── Market timing ────────────────────────────────────────────────────────
  "close_time":      "2025-05-07T16:00:00Z",  // RFC3339 market close/expiry
  // Exec uses this to trigger pre-expiry exits (HOURS_BEFORE_CLOSE config).
  // null for BTC-USD (no expiry).

  // ── FOMC-specific (null for non-FOMC) ────────────────────────────────────
  "meeting":         "2025-05",     // ISO year-month of FOMC meeting
  "outcome":         "CUT_25",      // "HOLD" | "CUT_25" | "CUT_50" | "HIKE_25" etc.
  "model_source":    "fedwatch+zq", // which data sources contributed
  "arb_partner":     null,          // paired ticker for 2-leg arb signals

  // ── Multi-leg arb (null for single-leg signals) ───────────────────────────
  // Set by scan_fomc_arb() for butterfly spread signals. Exec dispatches to
  // executor.execute_arb_legs() which places all legs atomically. On partial
  // fill, already-placed legs are best-effort cancelled.
  "arb_legs": null,
  // arb_legs structure when set (butterfly example):
  // [
  //   {"ticker": "KXFED-26JUN-T3.50", "side": "yes", "price": 0.30},  // buy leg A
  //   {"ticker": "KXFED-26JUN-T3.75", "side": "no",  "price": 0.65},  // sell leg B (overpriced middle)
  //   {"ticker": "KXFED-26JUN-T4.00", "side": "yes", "price": 0.18}   // buy leg C
  // ]
  // Each arb leg position in ep:positions carries "arb_id" and "arb_leg_index" metadata fields.

  // ── BTC mean-reversion (null for non-BTC) ────────────────────────────────
  "btc_price":       null,   // current spot price in USD
  "btc_z_score":     null,   // std deviations from rolling mean
  "btc_lookback_m":  null,   // rolling window in minutes

  // ── CME basis — future-ready (null until implemented) ────────────────────
  "futures_ticker":  null,   // e.g. "BTCM5"
  "basis_bps":       null,   // (futures_price - spot_price) / spot_price * 10000
  "carry_rate":      null    // annualised basis rate
}
```

---

### 2. `EXECUTION_REPORT` — fill confirmation

Published by Exec to `ep:executions` after every signal processed (filled OR rejected).
Intel reads these to update stats and confirm dedup state is correct.

```jsonc
{
  // ── Identity ────────────────────────────────────────────────────────────
  "exec_id":         "b8e2a4f9-...",   // UUIDv4 — unique per report
  "signal_id":       "a3f7c2d1-...",   // links back to SignalMessage.signal_id
  "schema_version":  "1",
  "msg_type":        "EXECUTION_REPORT",
  "source_node":     "exec-qvps-chi",
  "ts_us":           1718000000100000,

  // ── Result ───────────────────────────────────────────────────────────────
  "status": "filled",
  // status values:
  //   "filled"    — order placed (paper sim or live)
  //   "rejected"  — risk gate blocked it (see reject_reason)
  //   "expired"   — signal TTL had already passed when Exec read it
  //   "duplicate" — ticker already in Redis positions at time of processing
  //   "failed"    — HTTP or exchange error during order placement

  "reject_reason": null,
  // reject_reason values (set when status != "filled"):
  //   "EXPIRED"             ttl_ms exceeded
  //   "DUPLICATE"           ep:positions[ticker] already exists
  //   "BALANCE_UNKNOWN"     could not read Intel balance from Redis
  //   "RISK_GATE_SIZE"      Kelly sizing returned 0 contracts
  //   "RISK_GATE_SPREAD"    spread_cents exceeded max_spread_cents
  //   "RISK_GATE_EXPOSURE"  order would exceed market or total exposure limit
  //   "RISK_GATE_DRAWDOWN"  daily drawdown limit hit — trading halted
  //   "RISK_GATE_KALSHI"    generic Kalshi risk manager rejection
  //   "UNKNOWN_ASSET_CLASS" asset_class not handled by this Exec version
  //   "HTTP_ERROR"          network or exchange API error

  // ── Fill details (set when status == "filled") ───────────────────────────
  "ticker":       "KXFED-27APR-T4.25",
  "asset_class":  "kalshi",
  "side":         "yes",
  "contracts":    4,
  "fill_price":   0.72,   // actual fill price (may differ from signal market_price)
  "order_id":     "kalshi-order-abc123",  // exchange order ID; "paper" for simulated
  "mode":         "paper",  // "paper" | "live"

  // ── Cost accounting ───────────────────────────────────────────────────────
  "cost_cents":    288,   // fill_price * contracts * 100
  "fee_cents":     20,    // estimated fee on this trade
  "edge_captured": 0.13   // signal.edge at time of fill (for P&L attribution)
}
```

---

### 3. State entries in Redis Hashes (not stream messages)

#### `ep:positions` (HASH — Exec writes, Intel reads for dedup)

Key: `ticker`  
Value: JSON object

```jsonc
{
  "side":              "yes",
  "contracts":         4,
  "contracts_filled":  3,        // exchange-confirmed fills; used for exposure calc (fallback: contracts)
  "entry_cents":       72,       // ALWAYS stored as YES-market price × 100 regardless of side
  "fair_value":        0.85,
  "meeting":           "2025-05",
  "outcome":           "CUT_25",
  "close_time":        "2025-05-07T16:00:00Z",  // RFC3339 — used for pre-expiry exits
  "model_source":      "fedwatch+zq",
  "entered_at":        "2025-05-06T14:32:00Z",
  "pending":           false,    // true = pre-write crash guard; exit checker skips pending entries
  "order_id":          "abc-123", // Kalshi exchange order UUID; "paper" for simulated; "" if unknown
  "fill_confirmed":    true,     // fill_poll_loop sets this true when exchange confirms fill
  "high_water_pnl":    8,        // trailing-stop high-water mark in cents P&L
  "tranche_done":      0,        // 0=none, 1=first tranche exited (BTC or pre-expiry 50%)
  "asset_class":       "kalshi", // "kalshi" | "btc_spot"
  // Exit TIF escalation fields (set when a resting exit limit order is placed):
  "pending_exit":      false,    // true = exit limit order resting; skip re-exit until confirmed
  "exit_order_id":     null,     // order_id of the resting exit limit
  "exit_order_placed_at": null,  // ISO timestamp when exit was placed
  "exit_offer_cents":  null,     // current offer price of exit limit
  "exit_reason":       null,     // "TAKE_PROFIT" | "STOP_LOSS" | etc.
  "exit_widen_count":  0         // number of TIF widenings applied (max EXIT_TIF_MAX_STEPS=3)
}
```

**CRITICAL:** `entry_cents` always holds the YES-market price × 100 for both YES and NO positions. The exit P&L formula `move_cents = entry_cents - current_yes_cents` relies on this for correct sign direction on NO positions.

#### `ep:prices` (HASH — Intel writes, Exec reads for exit management)

Key: `ticker`  
Value: JSON object

```jsonc
{
  "yes_price":  72,
  "no_price":   28,
  "spread":     4,
  "last_price": 71,
  "ts_us":      1718000000000000
}
```

Exec uses `last_price` for take-profit / stop-loss calculations.
Any entry older than 300s (5 min) should be treated as stale by Exec.

#### `ep:balance` (HASH — both nodes write)

Key: `node_id`  
Value: JSON object

```jsonc
{
  "balance_cents": 100000,
  "mode":          "paper",
  "ts_us":         1718000000000000
}
```

Exec reads the Intel node's balance entry for risk sizing when it cannot
access the exchange directly (common in paper mode).

#### `ep:config` (HASH — ops writes for runtime overrides)

```
HALT_TRADING     "1"          # emergency stop — both nodes check this
EDGE_THRESHOLD   "0.12"       # override config.py value at runtime
MAX_CONTRACTS    "3"          # temporary size reduction
```

#### `ep:macro` (HASH — Intel writes every 120s)

Key-value pairs (all string):

```
fed_rate           "4.25"       # current DFEDTARU (%)
vix                "18.92"      # CBOE VIX
dgs10              "4.3"        # 10Y Treasury yield (%)
dgs2               "3.76"       # 2Y Treasury yield (%)
yield_curve_spread "0.51"       # dgs10 - dgs2
t10y2y             "0.51"       # FRED T10Y2Y
core_cpi_yoy       "2.67"       # FRED CPILFESL YoY (%)
pce_yoy            "2.80"       # FRED PCEPI YoY (%)
icsa               "214000"     # Initial jobless claims
t5yifr             "2.26"       # 5Y5Y forward inflation rate
gdpnow             "2.3"        # Atlanta Fed GDPNow (%)
move_index         "95.0"       # ICE MOVE bond vol index
next_fomc_date     "2026-05-07" # next FOMC meeting date (ISO)
ts                 "1745000000" # Unix timestamp of last refresh
```

#### `ep:releases` (HASH — Intel writes on BLS release day)

Only populated during the 30-min window around a BLS release (8:28–8:58 ET):

```
CPI          "3.1"           # CPI value from BLS (%)
CPI_period   "2026-03"       # period string from BLS response
CPI_ts       "1745000000"    # Unix timestamp of detection
NFP          "152000"        # Non-farm payrolls (thousands)
NFP_period   "2026-03"
NFP_ts       "1745000000"
```

#### `ep:divergence` (HASH — Exec writes hourly when ≥5 completed trades in 7d window)

```
realized_pnl_cents   "1450"    # sum of (exit - entry) * contracts - fee over window
expected_pnl_cents   "2100"    # sum of (fair_value - entry) * contracts (positive-only)
edge_capture_ratio   "0.6905"  # realized / expected; < 0.50 triggers Telegram alert
n_completed          "23"      # completed round-trips counted in window
window_days          "7"
ts                   "1745000000"
```

#### `ep:kelly_calib:strategy` (HASH — ep_kelly_calib writes every 4h from Postgres)

```
fedwatch+tbill+wsj   "1.12"   # confidence multiplier (>1 = outperforms; <1 = underperforms)
noaa_nws+open_meteo  "1.00"
calendar_decay       "0.95"
```

Multiplied into `confidence` during Kelly sizing. Requires ≥10 terminal trades per `model_source` bucket.

---

### 4. `SYSTEM` events — `ep:system` stream

Informational only. Neither Intel nor Exec acts on these; they are for monitoring.

```jsonc
{
  "event_type": "INTEL_START",   // INTEL_START | INTEL_STOP | EXEC_START | EXEC_STOP
                                 // DRAWDOWN_HALT | WS_RECONNECT | MARKET_RESCAN
  "node":       "intel-do-nyc3",
  "detail":     "mode=paper",
  "ts_us":      1718000000000000
}
```

---

## Ordering guarantees and delivery semantics

| Stream | Producer | Consumer group | Guarantee |
|---|---|---|---|
| `ep:signals` | Intel (1 producer) | `exec-consumers` | At-least-once delivery via XACK. Exec deduplicates on `ep:positions` hash. |
| `ep:executions` | Exec (1 producer) | `intel-consumers` | At-least-once. Intel deduplicates on `exec_id` if needed. |
| `ep:system` | Both | Read-only (monitoring) | Fire-and-forget. |

### TTL semantics

Signal TTL is enforced by Exec, not by Redis expiry. Redis Streams do not natively
expire individual entries. Exec checks `(now_us - ts_us) > ttl_ms * 1000` and
publishes an `ExecutionReport(status="expired")` without placing an order.

This means Intel can always audit what happened to every signal it published.

### Crash recovery

If Exec crashes mid-processing, the unconsumed stream entries remain in the consumer
group's PEL (Pending Entries List). On restart, Exec reads PEL entries first
(`XREADGROUP ... ID "0"` instead of `">"`) and re-processes them. Because dedup
is on `ep:positions`, re-processing a signal for a position already open produces
an `ExecutionReport(status="duplicate")` — no double entry.

---

## Versioning

`schema_version: "1"` is included in every message. When fields are added:
- Add with a default value (backward compatible — old consumers skip unknown fields)
- Increment version only for breaking changes (field removal or type change)
- Consumers MUST ignore unknown fields rather than error
