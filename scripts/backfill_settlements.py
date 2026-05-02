#!/usr/bin/env python3
"""
backfill_settlements.py — One-shot backfill of /portfolio/settlements
into position_history + trades.csv.

Usage:
    ./scripts/backfill_settlements.py --dry-run [--since 2026-04-01]
    ./scripts/backfill_settlements.py --apply   [--since 2026-04-01]

In --dry-run mode (default-safe), prints one line per settlement showing
action / cost-basis source / pnl. No writes are performed.

In --apply mode, refuses to run if `edgepulse-exec` is active to avoid
concurrent writes with the live settlement_recon_loop. Reuses
ep_settlements.reconcile_one_settlement so the dry-run/apply paths share
identical logic with the live loop.

Exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ep_config sets sys.path so kalshi_bot is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ep_config  # noqa: F401, E402
from ep_config import REDIS_URL, NODE_ID  # noqa: E402

import asyncpg  # noqa: E402

from ep_bus import RedisBus  # noqa: E402
from ep_pg_audit import init_audit_writer, stop_audit_writer  # noqa: E402
from ep_settlements import reconcile_one_settlement  # noqa: E402
from kalshi_bot.auth import KalshiAuth, NoAuth  # noqa: E402
from kalshi_bot.client import KalshiClient  # noqa: E402
from kalshi_bot.executor import Executor  # noqa: E402
import kalshi_bot.config as cfg  # noqa: E402

log = logging.getLogger("edgepulse")


_PAGE_LIMIT = 100


def _service_active(unit: str) -> bool:
    """Return True if `systemctl is-active <unit>` reports active."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


async def _fetch_all_settlements(client, since_iso: str) -> list[dict]:
    """Page through /portfolio/settlements until no more results."""
    try:
        since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        min_ts_epoch = int(since_dt.timestamp())
    except Exception as exc:
        raise RuntimeError(f"unparseable --since {since_iso!r}: {exc}") from exc

    all_settlements: list[dict] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": _PAGE_LIMIT, "min_ts": min_ts_epoch}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = await asyncio.to_thread(
                client.get, "/portfolio/settlements", params,
            )
        except Exception as exc:
            raise RuntimeError(f"Kalshi fetch failed: {exc}") from exc
        page = (resp or {}).get("settlements") or []
        all_settlements.extend(page)
        pages += 1
        cursor = (resp or {}).get("cursor")
        if not cursor or len(page) < _PAGE_LIMIT:
            break
    print(f"# fetched {len(all_settlements)} settlements across {pages} page(s) since {since_iso}",
          file=sys.stderr)
    return all_settlements


async def _run(args: argparse.Namespace) -> int:
    # 1. Build Kalshi auth + client.  Mirrors ep_exec.py:4216-4225.
    auth = (
        NoAuth()
        if (cfg.PAPER_TRADE and not cfg.API_KEY_ID)
        else KalshiAuth(api_key_id=cfg.API_KEY_ID,
                        private_key_path=cfg.PRIVATE_KEY_PATH)
    )
    client = KalshiClient(
        base_url    = cfg.BASE_URL,
        auth        = auth,
        timeout     = cfg.HTTP_TIMEOUT,
        max_retries = cfg.MAX_RETRIES,
        backoff     = cfg.RETRY_BACKOFF,
        concurrency = cfg.CONCURRENCY,
    )
    # Executor is needed in --apply mode for trades.csv writes via
    # _log_trade. In --dry-run we still build it (zero-cost; opens the
    # CSV file in append mode) so the call signature is uniform.
    # Args mirror ep_exec.py:4263-4271.
    executor = Executor(
        client             = client,
        trades_csv         = cfg.TRADES_CSV,
        paper              = cfg.PAPER_TRADE,
        take_profit_cents  = cfg.TAKE_PROFIT_CENTS,
        stop_loss_cents    = cfg.STOP_LOSS_CENTS,
        hours_before_close = cfg.HOURS_BEFORE_CLOSE,
        state              = None,
    )

    # 2. RedisBus — positional (url, node_id) per ep_bus.py:31.
    bus = RedisBus(REDIS_URL, NODE_ID)
    await bus.connect()
    bus_redis = bus._r

    # 3. Postgres pool for cost-basis lookup
    dsn = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    pool: asyncpg.Pool | None = None
    if dsn:
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2,
                                             command_timeout=10)
        except Exception as exc:
            print(f"WARN: Postgres pool init failed ({exc}); cost-basis "
                  "lookups will fall back to kalshi_agg", file=sys.stderr)
            pool = None
    else:
        print("WARN: DATABASE_URL unset — cost-basis lookups disabled",
              file=sys.stderr)

    # 4. In --apply mode, also init the audit writer so reconcile_one_settlement
    #    has a real PgAuditWriter to call .write() on.
    if args.apply:
        await init_audit_writer()

    # 5. Pull positions snapshot once
    try:
        positions_map = await bus.get_all_positions()
    except Exception as exc:
        print(f"WARN: get_all_positions failed: {exc}", file=sys.stderr)
        positions_map = {}

    # 6. Fetch + reconcile
    rc = 0
    try:
        settlements = await _fetch_all_settlements(client, args.since)
        actions = {"inserted": 0, "skipped_duplicate": 0, "dry_run": 0,
                   "paper_skip": 0, "malformed": 0}
        for s in settlements:
            pos_snap = positions_map.get(s.get("ticker") or "")
            try:
                result = await reconcile_one_settlement(
                    s, executor, bus_redis,
                    pool=pool, pos_snapshot=pos_snap,
                    dry_run=not args.apply,
                    paper_skip=True,
                    model_source="settlement_backfill",
                )
            except Exception as exc:
                rc = 1
                print(f"ERROR reconciling {s.get('ticker')}: {exc}",
                      file=sys.stderr)
                continue
            actions[result["action"]] = actions.get(result["action"], 0) + 1
            print(
                f"{result['action']:18s}  {result['ticker']:32s}  "
                f"src={result['cost_basis_source']:10s}  "
                f"pnl={result['pnl_cents']:+7d}c  {result['reason']}"
            )
        print("# summary: " + "  ".join(f"{k}={v}" for k, v in actions.items()),
              file=sys.stderr)
    finally:
        if args.apply:
            # Drain any queued audit rows before closing.
            try:
                await stop_audit_writer()
            except Exception:
                pass
        if pool is not None:
            await pool.close()
        await bus.close()

    return rc


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill /portfolio/settlements into position_history.",
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true",
                     help="Show what would be reconciled; no writes.")
    grp.add_argument("--apply", action="store_true",
                     help="Actually write to position_history + trades.csv.")
    p.add_argument("--since", default="2026-04-01",
                   help="Earliest settlement to consider (ISO 8601). "
                        "Default: 2026-04-01.")
    args = p.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.apply and _service_active("edgepulse-exec"):
        print(
            "ERROR: edgepulse-exec is active — refusing to run --apply "
            "(concurrent writes with settlement_recon_loop). "
            "Stop the service first or use --dry-run.",
            file=sys.stderr,
        )
        return 3

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
