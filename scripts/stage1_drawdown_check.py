"""
stage1_drawdown_check.py — Stage 1 experiment drawdown safety.

Compares total account value (cash + portfolio mark-to-market) against the
Stage 1 drawdown floor. If breached AND not already halted, sets
ep:config:HALT_TRADING=1 and records breach metadata.

Designed to run every 5 min via cron during the 14-day Stage 1 window.
Idempotent — safe to invoke repeatedly. Always exits 0 (cron-friendly).

Install:
    crontab -e
    */5 * * * * /usr/bin/python3 /root/EdgePulse/scripts/stage1_drawdown_check.py \
        >> /var/log/edgepulse/stage1_drawdown.log 2>&1

Reads:
    ep:balance                                  hash, intel-qvps-chi entry (JSON)
    ep:experiment:stage_1_drawdown_stop_cents   int floor
    ep:config:HALT_TRADING                      to avoid re-halting

Writes (only on first breach):
    ep:config:HALT_TRADING                              "1"
    ep:experiment:stage_1_drawdown_breached_ts          unix seconds
    ep:experiment:stage_1_drawdown_breached_value_cents observed total
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

SOCK = "/run/redis/redis.sock"
NODE = "intel-qvps-chi"


def _redis(*args: str) -> str | None:
    """redis-cli over the unix socket. Returns stdout stripped, or None on error."""
    try:
        out = subprocess.check_output(
            ["redis-cli", "-s", SOCK, *args],
            text=True, stderr=subprocess.PIPE, timeout=10,
        )
        return out.strip()
    except subprocess.CalledProcessError as e:
        print(f"[stage1_drawdown] redis-cli error: {(e.stderr or '').strip()}",
              file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("[stage1_drawdown] redis-cli timeout", file=sys.stderr)
        return None


def main() -> int:
    now_ts = int(time.time())
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw = _redis("hget", "ep:balance", NODE)
    if not raw:
        print(f"{now_iso}  WARN ep:balance:{NODE} missing — cannot evaluate")
        return 0
    try:
        bal = json.loads(raw)
    except json.JSONDecodeError:
        print(f"{now_iso}  WARN ep:balance:{NODE} not JSON: {raw[:80]}")
        return 0

    cash_cents = int(bal.get("balance_cents", 0))
    portfolio_cents = int(bal.get("portfolio_value_cents", 0))
    total_cents = cash_cents + portfolio_cents

    thresh_raw = _redis("hget", "ep:experiment", "stage_1_drawdown_stop_cents")
    if not thresh_raw:
        print(f"{now_iso}  WARN no stage_1_drawdown_stop_cents — Stage 1 not active?")
        return 0
    try:
        threshold = int(thresh_raw)
    except ValueError:
        print(f"{now_iso}  WARN threshold {thresh_raw!r} not int")
        return 0

    halt = _redis("hget", "ep:config", "HALT_TRADING")
    already_halted = (halt == "1")

    status = "OK" if total_cents >= threshold else "BREACH"
    print(
        f"{now_iso}  cash={cash_cents/100:>7.2f}  "
        f"portfolio={portfolio_cents/100:>6.2f}  "
        f"total={total_cents/100:>7.2f}  "
        f"floor={threshold/100:>6.2f}  "
        f"status={status}  halted={halt or '?'}"
    )

    if status == "BREACH" and not already_halted:
        _redis("hset", "ep:config", "HALT_TRADING", "1")
        _redis(
            "hset", "ep:experiment",
            "stage_1_drawdown_breached_ts", str(now_ts),
            "stage_1_drawdown_breached_value_cents", str(total_cents),
        )
        print(
            f"{now_iso}  *** HALT_TRADING=1 set — drawdown breached: "
            f"${total_cents/100:.2f} < ${threshold/100:.2f} ***"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
