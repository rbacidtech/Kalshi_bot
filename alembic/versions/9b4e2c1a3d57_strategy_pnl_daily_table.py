"""add strategy_pnl_daily table (Phase 2 A.5)

Revision ID: 9b4e2c1a3d57
Revises: 7a3b8f9c2e6d
Create Date: 2026-05-14

Per Engineering A.5: position-level P&L attribution by strategy, with daily
append-only snapshots. The `strategy_pnl_daily` table is the audit trail
that B.4's tuning loop, S.3's per-strategy circuit breaker, and A.4's cap
recalibration all read for empirical decisions.

Schema designed for the (date, strategy) primary key so each day's per-
strategy stats are exactly one row. Realtime intra-day counters live in
Redis at ep:strategy_pnl_realtime; this table receives the EOD snapshot
at 00:00 UTC rollover.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "9b4e2c1a3d57"
down_revision: Union[str, Sequence[str], None] = "7a3b8f9c2e6d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_pnl_daily (
            date                  DATE    NOT NULL,
            strategy              TEXT    NOT NULL,
            realized_pnl_cents    BIGINT  NOT NULL DEFAULT 0,
            unrealized_eod_cents  BIGINT  NOT NULL DEFAULT 0,
            fees_cents            BIGINT  NOT NULL DEFAULT 0,
            slippage_cents        BIGINT  NOT NULL DEFAULT 0,
            signal_count          INTEGER NOT NULL DEFAULT 0,
            fill_count            INTEGER NOT NULL DEFAULT 0,
            settlement_count      INTEGER NOT NULL DEFAULT 0,
            inserted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (date, strategy)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_pnl_daily_strategy "
        "ON strategy_pnl_daily (strategy, date DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_strategy_pnl_daily_strategy")
    op.execute("DROP TABLE IF EXISTS strategy_pnl_daily")
