"""add slippage decomposition columns to executions (Engineering B.2)

Revision ID: c8d2e5f1b943
Revises: 9b4e2c1a3d57
Create Date: 2026-05-14

Extends Phase 1.4 S.1.1's total `slippage_cents` column with 3 new
decomposition components per Engineering B.2:

  - spread_cost_cents     — half bid-ask premium paid per round trip
  - adverse_move_cents    — price drift during unfilled order lifetime
  - partial_fill_cents    — didn't get requested size; opportunity cost

cancel_replace_cents is intentionally NOT added in this commit — that
4th component requires order-lifecycle event logging (placement →
cancel → replace) which isn't yet captured. Deferred per Engineering
B.2 §.

All 3 new columns nullable so pre-migration rows aren't disturbed.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c8d2e5f1b943"
down_revision: Union[str, Sequence[str], None] = "9b4e2c1a3d57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE executions ADD COLUMN IF NOT EXISTS spread_cost_cents BIGINT NULL")
    op.execute("ALTER TABLE executions ADD COLUMN IF NOT EXISTS adverse_move_cents BIGINT NULL")
    op.execute("ALTER TABLE executions ADD COLUMN IF NOT EXISTS partial_fill_cents BIGINT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE executions DROP COLUMN IF EXISTS partial_fill_cents")
    op.execute("ALTER TABLE executions DROP COLUMN IF EXISTS adverse_move_cents")
    op.execute("ALTER TABLE executions DROP COLUMN IF EXISTS spread_cost_cents")
