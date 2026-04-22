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
