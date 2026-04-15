"""
ep_positions.py — PositionStore: Redis-backed replacement for paper_positions.json.

Both Intel (reads for dedup) and Exec (writes after fills) share one view
via the ep:positions Redis hash.
"""

from datetime import datetime, timezone
from typing import Dict, Optional

from ep_config import log
from ep_bus import RedisBus


class PositionStore:

    def __init__(self, bus: RedisBus):
        self._bus = bus

    async def open(
        self,
        ticker:      str,
        side:        str,
        contracts:   int,
        entry_cents: int,
        fair_value:  float,
        meeting:     str = "",
        outcome:     str = "",
        close_time:  str = "",
    ) -> None:
        pos = {
            "side":        side,
            "contracts":   contracts,
            "entry_cents": entry_cents,
            "fair_value":  fair_value,
            "meeting":     meeting,
            "outcome":     outcome,
            "close_time":  close_time,
            "entered_at":  datetime.now(timezone.utc).isoformat(),
        }
        await self._bus.set_position(ticker, pos)
        log.debug("Position opened: %s  side=%s  contracts=%d", ticker, side, contracts)

    async def close(self, ticker: str) -> Optional[dict]:
        """Remove position and return the stored dict (used for P&L calculation)."""
        all_pos = await self._bus.get_all_positions()
        pos = all_pos.get(ticker)
        await self._bus.delete_position(ticker)
        log.debug("Position closed: %s", ticker)
        return pos

    async def exists(self, ticker: str) -> bool:
        return await self._bus.position_exists(ticker)

    async def get_all(self) -> Dict[str, dict]:
        return await self._bus.get_all_positions()

    async def total_exposure_cents(self) -> int:
        """Sum of (entry_cents × contracts) across all open positions."""
        positions = await self._bus.get_all_positions()
        return sum(
            p.get("entry_cents", 50) * p.get("contracts", 1)
            for p in positions.values()
        )
