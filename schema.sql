-- EdgePulse analytics schema
-- Apply: psql -h 127.0.0.1 -U edgepulse -d edgepulse -f schema.sql
-- Idempotent — safe to re-run.

-- Every signal ever published
CREATE TABLE IF NOT EXISTS signals (
    signal_id       UUID PRIMARY KEY,
    emitted_at      TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    market_price    NUMERIC(10,4),
    fair_value      NUMERIC(10,4),
    edge            NUMERIC(10,4),
    confidence      NUMERIC(5,4),
    suggested_size  INTEGER,
    payload         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_emitted   ON signals (emitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_strategy  ON signals (strategy, emitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_ticker    ON signals (ticker, emitted_at DESC);

-- Every execution report (filled or rejected)
CREATE TABLE IF NOT EXISTS executions (
    exec_id         UUID PRIMARY KEY,
    signal_id       UUID,   -- no FK: async batching means execution can land before its signal
    reported_at     TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL,
    reject_reason   TEXT,
    ticker          TEXT,
    side            TEXT,
    asset_class     TEXT,
    contracts       INTEGER,
    fill_price      NUMERIC(10,4),
    fee_cents       BIGINT,
    cost_cents      BIGINT,
    edge_captured   NUMERIC(10,4),
    order_id        TEXT,
    mode            TEXT,
    payload         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_reported ON executions (reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_status   ON executions (status, reported_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_reject   ON executions (reject_reason, reported_at DESC)
    WHERE reject_reason IS NOT NULL;

-- Daily balance snapshots
CREATE TABLE IF NOT EXISTS balance_snapshots (
    snap_id         BIGSERIAL PRIMARY KEY,
    taken_at        TIMESTAMPTZ NOT NULL,
    asset_class     TEXT NOT NULL,
    balance_cents   BIGINT NOT NULL,
    open_pos_count  INTEGER NOT NULL,
    exposure_cents  BIGINT NOT NULL,
    daily_pnl_cents BIGINT
);
CREATE INDEX IF NOT EXISTS idx_balance_taken ON balance_snapshots (taken_at DESC, asset_class);

-- LLM agent decisions
CREATE TABLE IF NOT EXISTS llm_decisions (
    decision_id       BIGSERIAL PRIMARY KEY,
    decided_at        TIMESTAMPTZ NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    config_before     JSONB NOT NULL,
    config_after      JSONB NOT NULL,
    reasoning         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_decided ON llm_decisions (decided_at DESC);

-- Position lifecycle (one row per closed position — used for Kelly calibration)
-- entry_exec_id links back to the fill row in executions (no FK — same async reason as signal_id)
CREATE TABLE IF NOT EXISTS position_history (
    hist_id              BIGSERIAL PRIMARY KEY,
    entry_exec_id        UUID NOT NULL,
    ticker               TEXT NOT NULL,
    side                 TEXT NOT NULL,
    contracts            INTEGER NOT NULL,
    entry_cents          INTEGER NOT NULL,
    exit_cents           INTEGER NOT NULL,
    realized_pnl_cents   BIGINT NOT NULL,
    exit_reason          TEXT,
    entered_at           TIMESTAMPTZ,
    exited_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_poshistory_exec    ON position_history (entry_exec_id);
CREATE INDEX IF NOT EXISTS idx_poshistory_exited  ON position_history (exited_at DESC);
CREATE INDEX IF NOT EXISTS idx_poshistory_ticker  ON position_history (ticker, exited_at DESC);

-- Terminal-trades view: join signal metadata → fill → outcome for Kelly recalibration.
-- "Terminal" exits (resolution, pre-expiry) give clean empirical win rates per edge bucket.
CREATE OR REPLACE VIEW terminal_trades AS
SELECT
    s.signal_id,
    s.strategy,
    s.asset_class,
    s.ticker,
    s.emitted_at,
    s.edge                          AS stated_edge,
    s.confidence                    AS stated_confidence,
    s.market_price                  AS entry_price,
    e.fill_price                    AS actual_entry,
    ph.contracts,
    ph.exit_cents / 100.0           AS actual_exit,
    ph.realized_pnl_cents,
    ph.exit_reason,
    ph.exited_at,
    CASE WHEN ph.exit_reason IN ('resolution', 'pre_expiry_full', 'near_certain_resolve')
         THEN true ELSE false END   AS is_terminal,
    -- Return as a fraction of entry cost (positive = win, negative = loss)
    (ph.exit_cents - (e.fill_price * 100)) / NULLIF(e.fill_price * 100, 0) AS return_frac
FROM signals s
JOIN executions e USING (signal_id)
JOIN position_history ph ON ph.entry_exec_id = e.exec_id
WHERE e.status = 'filled'
  AND ph.exited_at IS NOT NULL;

-- Market snapshot: every market Intel sees every 120 s scan cycle.
-- This is the backtest dataset — all open Kalshi markets, bid/ask/mid/spread/volume,
-- plus any signal generated for that ticker that cycle.
-- ~500 markets × 30 cycles/h → ~10 M rows/month; BRIN keeps index tiny.
CREATE TABLE IF NOT EXISTS market_snapshots (
    snap_id       BIGSERIAL PRIMARY KEY,
    ts_us         BIGINT       NOT NULL,         -- microseconds UTC
    ticker        TEXT         NOT NULL,
    series_ticker TEXT,
    yes_bid       SMALLINT,                      -- cents 0-100 (REST yes_bid_dollars × 100)
    yes_ask       SMALLINT,
    yes_price     SMALLINT,                      -- mid cents; WS value preferred over REST
    spread        SMALLINT,                      -- yes_ask - yes_bid, cents
    volume        INTEGER,                       -- total contracts traded (REST, ~20 min stale)
    open_interest INTEGER,
    close_time    TIMESTAMPTZ,                   -- market expiry
    signal_edge   NUMERIC(6,4),                  -- null when no signal this cycle
    signal_side   TEXT,
    signal_fv     NUMERIC(6,4),
    signal_conf   NUMERIC(5,4)
);
-- BRIN: 8 KB per 128 pages instead of MBs — ideal for sequential-append time-series
CREATE INDEX IF NOT EXISTS idx_msnap_ts     ON market_snapshots USING BRIN (ts_us);
-- Per-ticker time-series (backtest queries: "give me KXFED-25MAY21 prices over last 3 months")
CREATE INDEX IF NOT EXISTS idx_msnap_ticker ON market_snapshots (ticker, ts_us DESC);

-- Rejection time-series: Grafana can query this directly for rate panels grouped by reason.
-- Example: SELECT hour, reject_reason, n FROM rejections_hourly WHERE hour > now()-'24h'::interval
CREATE OR REPLACE VIEW rejections_hourly AS
SELECT
    date_trunc('hour', e.reported_at)  AS hour,
    e.reject_reason,
    e.asset_class,
    s.strategy,
    count(*)                            AS n
FROM executions e
LEFT JOIN signals s USING (signal_id)
WHERE e.status = 'rejected'
  AND e.reported_at > now() - INTERVAL '90 days'
GROUP BY 1, 2, 3, 4
ORDER BY 1 DESC, 5 DESC;

-- Quick inspection: which reasons dominate today vs the 7-day baseline?
-- A reason whose 24h share is 2× its 7d share is a signal something changed.
CREATE OR REPLACE VIEW rejection_distribution AS
SELECT
    reject_reason,
    asset_class,
    count(*) FILTER (WHERE reported_at > now() - INTERVAL '1 day')  AS last_24h,
    count(*) FILTER (WHERE reported_at > now() - INTERVAL '7 days') AS last_7d,
    round(
        100.0
        * count(*) FILTER (WHERE reported_at > now() - INTERVAL '1 day')
        / NULLIF(count(*) FILTER (WHERE reported_at > now() - INTERVAL '7 days'), 0),
        1
    )                                                                AS pct_24h_of_7d
FROM executions
WHERE status = 'rejected'
  AND reported_at > now() - INTERVAL '7 days'
GROUP BY reject_reason, asset_class
ORDER BY last_24h DESC;

-- Active refresh token lookup (most auth queries filter WHERE NOT revoked)
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_active
    ON refresh_tokens (user_id, expires_at)
    WHERE NOT revoked;

-- Autovacuum: fire at 2% dead rows (vs default 20%) on append-heavy tables
ALTER TABLE audit_logs        SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE pnl_snapshots     SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE signals           SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE executions        SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE balance_snapshots SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE llm_decisions     SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE position_history  SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE market_snapshots  SET (autovacuum_vacuum_scale_factor = 0.02,
                                   autovacuum_analyze_scale_factor = 0.01,
                                   autovacuum_vacuum_cost_delay    = 2);
