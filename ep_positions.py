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
    """High-level wrapper around RedisBus position operations.

    Provides open/close/update semantics with guard logic (e.g. never overwrite
    a confirmed position with a pending pre-write).
    """

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
        confidence:   float = 0.0,
        pending:      bool = False,
        category:     str = "",
    ) -> None:
        """Open a new position in Redis.

        Guards against overwriting a confirmed (non-pending) position.
        entry_cents must be the YES price equivalent (0-100), regardless of side.
        """
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

        # entry_cents convention: always the YES price (0-100), never the raw NO price.
        # A NO position with entry_cents > 95 means YES was at 95¢+ at entry, which
        # is extremely rare and usually indicates the NO price was stored directly
        # (e.g. NO@95¢ stored as 95 instead of the correct YES-equivalent of 5).
        # KXFED / butterfly arb NO entries with YES in the 70-94¢ range are normal.
        if side == "no" and entry_cents > 95:
            from ep_config import log as _log
            _log.error(
                "entry_cents suspicious: NO position on %s has entry_cents=%d > 95. "
                "Verify YES equivalent was passed (not NO price). P&L direction may be wrong.",
                ticker, entry_cents,
            )
            try:
                from ep_metrics import metrics as _m
                _m.record_invariant_violation("entry_cents_no_gt_95")
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
            "confidence":        confidence,
            "category":          category,
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
        """Return True if any position (pending or confirmed) exists for this ticker."""
        return await self._bus.position_exists(ticker)

    async def get_all(self) -> Dict[str, dict]:
        """Return all open positions keyed by ticker."""
        return await self._bus.get_all_positions()

    async def add_contracts(
        self,
        ticker:           str,
        added_contracts:  int,
        added_entry_cents: int,
    ) -> bool:
        """Merge a top-up fill into an existing position.

        Increments contracts/contracts_filled and recomputes the YES-price
        weighted-average entry_cents so P&L attribution stays correct for
        the aggregate position. Resets high_water_pnl and tranche_done so
        exit logic treats the merged total as a fresh position, and clears
        pending_topup so the next top-up signal can proceed.

        Returns True on success, False if the position is missing or the
        top-up count is non-positive.
        """
        if added_contracts <= 0:
            return False
        all_pos = await self._bus.get_all_positions()
        pos = all_pos.get(ticker)
        if pos is None:
            return False
        old_ct = int(pos.get("contracts", 0))
        old_fl = int(pos.get("contracts_filled", 0))
        old_en = int(pos.get("entry_cents", 0))
        if old_ct <= 0:
            return False
        new_ct = old_ct + added_contracts
        new_fl = old_fl + added_contracts
        new_en = int(round(
            (old_en * old_ct + added_entry_cents * added_contracts) / new_ct
        ))
        pos.update({
            "contracts":        new_ct,
            "contracts_filled": new_fl,
            "entry_cents":      new_en,
            "high_water_pnl":   0,
            "tranche_done":     0,
            "pending_topup":    None,
        })
        await self._bus.set_position(ticker, pos)
        log.info(
            "Position topped up: %s  +%d → %d contracts  avg_entry=%d¢ (was %d¢)",
            ticker, added_contracts, new_ct, new_en, old_en,
        )
        return True

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
        """Sum of actual contract cost across all FILLED positions.

        entry_cents stores the YES market price (0–100) for all positions.
        For NO contracts the capital deployed is (100 - entry_cents) per contract,
        not entry_cents — using YES price would overstate cheap NOs and understate
        in-the-money NOs, both causing wrong risk-gate decisions.

        Unfilled resting limit orders are excluded. Kalshi reserves their cost
        against `balance` already, so counting them here would double-count
        them (once in this numerator, once by reducing the cash denominator).
        The account-value denominator used by the approve() call is stable
        across fills — see ep_exec.py for the pairing.
        """
        positions = await self._bus.get_all_positions()
        total = 0
        for p in positions.values():
            if p.get("user_bet"):
                continue  # personal bets are not bot capital
            entry = p.get("entry_cents", 50)
            filled = int(p.get("contracts_filled") or 0)
            # Skip if nothing has filled yet — treat as resting order, not exposure.
            # An unfilled position has contracts_filled=0 AND fill_confirmed=False.
            if filled == 0 and not p.get("fill_confirmed"):
                continue
            contracts = filled or int(p.get("contracts", 1))
            if p.get("side") == "no":
                total += (100 - entry) * contracts
            else:
                total += entry * contracts
        return total
