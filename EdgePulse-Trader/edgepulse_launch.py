#!/usr/bin/env python3
"""
edgepulse_launch.py — EdgePulse-Trader entry point.

Two operating modes, same repo, deployed via git pull:

    MODE=intel   DigitalOcean Droplet (NYC3)
                 Fetch data → generate signals → publish prices + signals to Redis

    MODE=exec    QuantVPS Chicago
                 Consume Redis signals → risk gate → execute orders → report fills

Deploy from Termux:
    git push origin main
    # on each node:
    git pull origin main && source venv/bin/activate
    MODE=intel NODE_ID=intel-do-nyc3  nohup python3 edgepulse_launch.py &
    MODE=exec  NODE_ID=exec-qvps-chi  nohup python3 edgepulse_launch.py &

Module map:
    ep_config.py     — runtime config, Redis key namespace, sys.path bootstrap
    ep_schema.py     — SignalMessage / ExecutionReport / PriceSnapshot dataclasses
    ep_bus.py        — RedisBus (stream + hash I/O)
    ep_positions.py  — PositionStore (Redis-backed position state)
    ep_risk.py       — UnifiedRiskEngine (Kalshi + BTC + CME basis)
    ep_adapters.py   — Signal ↔ SignalMessage translation
    ep_intel.py      — Intel main loop
    ep_exec.py       — Exec helpers + main loop

Schema reference: SCHEMA.md
"""

import asyncio
import logging

# ep_config must be imported first — sets sys.path so kalshi_bot is importable
from ep_config import MODE, NODE_ID, log


def main() -> None:
    logging.basicConfig(
        level    = logging.INFO,
        format   = "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
        handlers = [logging.StreamHandler()],
    )
    # Suppress noisy library logs
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if MODE == "intel":
        log.info("Starting EdgePulse in INTEL mode (node=%s)", NODE_ID)
        from ep_intel import intel_main    # lazy — Exec node never loads Intel deps
        asyncio.run(intel_main())
    elif MODE == "exec":
        log.info("Starting EdgePulse in EXEC mode (node=%s)", NODE_ID)
        from ep_exec import exec_main      # lazy — Intel node never loads Exec deps
        asyncio.run(exec_main())
    else:
        raise SystemExit(
            f"Unknown MODE={MODE!r}\n"
            f"Set MODE=intel (DO Droplet) or MODE=exec (QuantVPS Chicago)"
        )


if __name__ == "__main__":
    main()
