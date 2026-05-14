"""add slippage_cents column to executions (Phase 1.4 S.1.1)

Revision ID: 7a3b8f9c2e6d
Revises: c3a17fb9e2d4
Create Date: 2026-05-14

Adds `slippage_cents BIGINT NULL` to the executions table. Populated by
ep_pg_audit.py on INSERT from market_price_at_signal (new field on
ExecutionReport) and fill_price, side-aware:

    YES: slippage = (fill_price - market_price_at_signal) * 100 * contracts
    NO:  slippage = (market_price_at_signal - fill_price) * 100 * contracts

Positive = adverse (paid more than market quote at signal time).
Negative = favorable.
NULL = pre-migration row, or status != "filled", or market_price_at_signal
       unset (e.g. paths that didn't carry over signal context).

Migration is re-run-safe via IF NOT EXISTS.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "7a3b8f9c2e6d"
down_revision: Union[str, Sequence[str], None] = "c3a17fb9e2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE executions ADD COLUMN IF NOT EXISTS slippage_cents BIGINT NULL")
    # Partial index — only filled rows with computed slippage are queryable
    # for per-strategy aggregates (Phase 1.4 S.1.3).
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_exec_slippage "
        "ON executions (reported_at DESC) "
        "WHERE slippage_cents IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_exec_slippage")
    op.execute("ALTER TABLE executions DROP COLUMN IF EXISTS slippage_cents")
