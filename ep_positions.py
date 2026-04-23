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
        ticker:       str,
        side:         str,
        contracts:    int,
        entry_cents:  int,
        fair_value:   float,
        meeting:      str = "",
        outcome:      str = "",
        close_time:   str = "",
        model_source: str = "",
        pending:      bool = False,
    ) -> None:
        # Guard: never overwrite a confirmed (non-pending) position with a new
        # pending pre-write.  This prevents crash-recovery pre-writes from
        # clobbering live position data that was manually registered or entered
        # by a previous cycle and still has a real Kalshi order behind it.
        existing = (await self._bus.get_all_positions()).get(ticker)
        if existing and not existing.get("pending", False):
            log.debug(
                "Position open skipped: %s already confirmed — not overwriting", ticker
            )
            return

        # entry_cents must always be the YES price (0–100 scale), never the NO price.
        # For a NO position, entry_cents > 70 is suspicious: you'd typically only hold NO
        # when YES > 70 if you have a very strong contrarian thesis.  More likely the NO
        # price was passed instead of the YES price, which inverts P&L direction.
        # Log a visible error but do NOT raise — valid contrarian trades can have YES > 50.
        if side == "no" and entry_cents > 70:
            from ep_config import log as _log
            _log.error(
                "entry_cents suspicious: NO position on %s has entry_cents=%d > 70. "
                "Verify YES price was passed (not NO price). P&L direction may be wrong.",
                ticker, entry_cents,
            )
            try:
                from ep_metrics import metrics as _m
                _m.record_invariant_violation("entry_cents_no_gt_70")
            except Exception:
                pass

        pos = {
            "side":              side,
            "contracts":         contracts,
            "contracts_filled":  0,        # updated by fill_poll as exchange reports fills
            "entry_cents":       entry_cents,
            "fair_value":        fair_value,
            "meeting":           meeting,
            "outcome":           outcome,
            "close_time":        close_time,
            "model_source":      model_source,
            "entered_at":        datetime.now(timezone.utc).isoformat(),
            "pending":           pending,
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

    async def update_fields(self, ticker: str, updates: dict) -> None:
        """
        Merge `updates` into an existing position without closing it.
        Used for trailing-stop HWM, pre-expiry tranche counter, and
        pending-position confirmation.
        """
        all_pos = await self._bus.get_all_positions()
        pos = all_pos.get(ticker)
        if pos is None:
            return
        pos.update(updates)
        await self._bus.set_position(ticker, pos)

    async def total_exposure_cents(self) -> int:
        """Sum of actual contract cost across all open positions.

        entry_cents stores the YES market price (0–100) for all positions.
        For NO contracts the capital deployed is (100 - entry_cents) per contract,
        not entry_cents — using YES price would overstate cheap NOs and understate
        in-the-money NOs, both causing wrong risk-gate decisions.
        """
        positions = await self._bus.get_all_positions()
        total = 0
        for p in positions.values():
            entry     = p.get("entry_cents", 50)
            # Use contracts_filled when available (partial-fill window);
            # falls back to contracts (requested size) for pending/unfilled orders.
            contracts = p.get("contracts_filled") or p.get("contracts", 1)
            if p.get("side") == "no":
                total += (100 - entry) * contracts
            else:
                total += entry * contracts
        return total
