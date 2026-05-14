"""Hard halt mechanism — Phase 1.3 S.3.4 of EdgePulse_Migration_Plan_2026.md.

Distinct from the soft halt (`ep:config:HALT_TRADING`):
  - Soft halt: pauses new entries. Cleared by `redis-cli hset HALT_TRADING 0`.
    Tripped automatically by drawdown breaker, daily P&L loss (S.3.1), and
    balance velocity (S.3.2).
  - Hard halt: pauses new entries AND cancels all resting orders. Requires
    operator to remove the filesystem flag file to clear (no Redis-only
    un-halt path; protects against accidental un-halt via misclick or LLM).

Operational interface:
  - Trip:   `touch /root/EdgePulse/.hard_halt` (operator) OR `set_hard_halt()`
            (programmatic, in catastrophic scenarios — not currently wired
            as an automatic trigger; mechanism only per Migration Plan §4.3).
  - Check:  `is_hard_halted()` returns bool; cheap (~1ms filesystem stat).
  - Clear:  `rm /root/EdgePulse/.hard_halt` AND `redis-cli hdel ep:halt
            hard_halt_*`. Service-level clear command may follow in a
            subsequent migration step.

Persistence: filesystem flag survives Redis loss, service restart, and
process crash. Migration Plan §11 Stop/Continue Decision Gates calls this
out explicitly as a defense against Redis-availability single-point failures.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Standard location alongside .deployed_sha and .parity_override.log
HARD_HALT_FLAG: Path = Path(os.environ.get("EP_HARD_HALT_FLAG", "/root/EdgePulse/.hard_halt"))


def is_hard_halted() -> bool:
    """Cheap filesystem-stat check. Returns True iff the flag file exists."""
    try:
        return HARD_HALT_FLAG.exists()
    except Exception:
        # On filesystem error, default to NOT halted — better to risk a few
        # trades than block forever on a transient stat failure.
        return False


def set_hard_halt(reason: str, *, kalshi_client: Optional[Any] = None) -> dict:
    """Trip the hard halt: write flag file, optionally cancel resting orders.

    Returns a dict with `tripped_at_us`, `flag_path`, `cancelled_orders` count,
    and `cancel_errors` count. Idempotent: subsequent calls update the flag
    file's mtime but don't double-cancel orders (the flag already existing
    is the de-dup signal).
    """
    now_us = int(time.time() * 1_000_000)
    already = HARD_HALT_FLAG.exists()
    try:
        HARD_HALT_FLAG.write_text(f"reason={reason}\ntripped_at_us={now_us}\n")
    except Exception as exc:
        log.error("Hard halt: failed to write flag file %s: %s", HARD_HALT_FLAG, exc)
        # If we can't write the flag, we still try to cancel orders below —
        # imperfect but better than no defense.

    cancelled = 0
    errors = 0
    if not already and kalshi_client is not None:
        try:
            resp = kalshi_client.get("/portfolio/orders", {"status": "resting"})
            resting = resp.get("orders", []) if isinstance(resp, dict) else []
            for order in resting:
                oid = order.get("order_id") if isinstance(order, dict) else None
                if not oid:
                    continue
                try:
                    kalshi_client._request("DELETE", f"/portfolio/orders/{oid}")
                    cancelled += 1
                except Exception as e:
                    errors += 1
                    log.warning("Hard halt: cancel failed for order %s: %s", oid, e)
        except Exception as exc:
            errors += 1
            log.error("Hard halt: failed to list resting orders: %s", exc)

    log.warning(
        "HARD HALT %s — reason=%s, cancelled=%d resting orders, errors=%d. "
        "Clear with: rm %s",
        "RE-AFFIRMED" if already else "TRIPPED",
        reason, cancelled, errors, HARD_HALT_FLAG,
    )
    return {
        "tripped_at_us":     now_us,
        "flag_path":         str(HARD_HALT_FLAG),
        "cancelled_orders":  cancelled,
        "cancel_errors":     errors,
        "was_already_set":   already,
    }


def clear_hard_halt() -> bool:
    """Remove the flag file. Returns True iff the file was present before clearing.

    Intended for operator scripts and shutdown handlers — NOT auto-called by
    any bot loop. Per Migration Plan §4.3: "requiring manual flag-file touch
    to clear" — programmatic clear paths are intentionally absent from the
    automated trigger conditions.
    """
    try:
        if HARD_HALT_FLAG.exists():
            HARD_HALT_FLAG.unlink()
            log.warning("Hard halt cleared (flag file removed).")
            return True
        return False
    except Exception as exc:
        log.error("Hard halt: failed to remove flag file %s: %s", HARD_HALT_FLAG, exc)
        return False
