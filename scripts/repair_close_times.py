#!/usr/bin/env python3
"""
repair_close_times.py — One-shot script that patches empty close_time in ep:positions.

For each Kalshi position with close_time == "", fetches the market from the Kalshi
REST API and writes the correct close_time back to Redis.  Safe to re-run.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv("/etc/edgepulse/edgepulse.env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kalshi_bot.auth import KalshiAuth

BASE_URL      = os.getenv("KALSHI_BASE_URL",     "https://api.elections.kalshi.com/trade-api/v2")
API_KEY_ID    = os.getenv("KALSHI_API_KEY_ID",   "")
PRIVATE_KEY   = Path(os.getenv("KALSHI_PRIVATE_KEY_PATH", "/root/EdgePulse/private_key.pem"))
REDIS_URL     = os.getenv("REDIS_URL",           "redis://localhost:6379/0")


async def main() -> None:
    if not API_KEY_ID or not PRIVATE_KEY.exists():
        print("ERROR: Kalshi credentials not configured.", file=sys.stderr)
        sys.exit(1)

    auth = KalshiAuth(API_KEY_ID, PRIVATE_KEY)
    r    = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True,
                                   socket_connect_timeout=5)

    positions = await r.hgetall("ep:positions")
    missing = []
    for ticker, raw in positions.items():
        try:
            pos = json.loads(raw)
        except Exception:
            continue
        if not pos.get("close_time"):
            missing.append(ticker)

    if not missing:
        print("All positions already have close_time. Nothing to do.")
        await r.aclose()
        return

    print(f"Repairing close_time for {len(missing)} positions: {missing}\n")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        for ticker in missing:
            path    = f"/trade-api/v2/markets/{ticker}"
            headers = auth.sign("GET", path)
            try:
                resp = await client.get(f"/markets/{ticker}", headers=headers)
                if resp.status_code != 200:
                    print(f"  WARN {ticker}: HTTP {resp.status_code} — skipping")
                    continue
                market     = resp.json().get("market", {})
                close_time = market.get("close_time") or market.get("expiration_time") or ""
                if not close_time:
                    print(f"  WARN {ticker}: API returned no close_time — skipping")
                    continue

                raw_pos = await r.hget("ep:positions", ticker)
                if raw_pos is None:
                    print(f"  WARN {ticker}: position disappeared from Redis — skipping")
                    continue
                pos = json.loads(raw_pos)
                pos["close_time"] = close_time
                await r.hset("ep:positions", ticker, json.dumps(pos))
                print(f"  OK    {ticker}: close_time = {close_time}")

            except Exception as exc:
                print(f"  ERROR {ticker}: {exc}")

    await r.aclose()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
