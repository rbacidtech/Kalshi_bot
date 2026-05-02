"""add settlement columns to position_history

Revision ID: a46fc116f0bd
Revises: 6b147b663e41
Create Date: 2026-05-02

Adds 4 nullable columns + a partial unique index on position_history so the
Phase-3 settlement reconciliation (ep_settlements.py) can write rows scoped
by (ticker, settlement_ts).

The position_history table is bootstrapped by `psql -f schema.sql`, NOT by
Alembic's autogenerate (Alembic only manages auth/API tables — see
alembic/env.py:23-25). Both schema.sql and this migration apply the same
changes and use IF NOT EXISTS / IF EXISTS so re-running is harmless.

upgrade()/downgrade() emit raw SQL via op.execute() rather than op.add_column
because the table is not in Base.metadata.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = 'a46fc116f0bd'
down_revision: Union[str, Sequence[str], None] = '6b147b663e41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE position_history ADD COLUMN IF NOT EXISTS settlement_ts        TIMESTAMPTZ NULL")
    op.execute("ALTER TABLE position_history ADD COLUMN IF NOT EXISTS cost_basis_source    TEXT NULL")
    op.execute("ALTER TABLE position_history ADD COLUMN IF NOT EXISTS kalshi_fee_cents     BIGINT NULL")
    op.execute("ALTER TABLE position_history ADD COLUMN IF NOT EXISTS kalshi_revenue_cents BIGINT NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS position_history_settlement_uniq "
        "ON position_history (ticker, settlement_ts) "
        "WHERE settlement_ts IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS position_history_settlement_uniq")
    op.execute("ALTER TABLE position_history DROP COLUMN IF EXISTS kalshi_revenue_cents")
    op.execute("ALTER TABLE position_history DROP COLUMN IF EXISTS kalshi_fee_cents")
    op.execute("ALTER TABLE position_history DROP COLUMN IF EXISTS cost_basis_source")
    op.execute("ALTER TABLE position_history DROP COLUMN IF EXISTS settlement_ts")
