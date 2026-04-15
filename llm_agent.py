"""
llm_agent.py — Claude-driven policy generator for EdgePulse.

Runs as a STANDALONE async script on the DO Droplet (NOT in the hot path).

Every LLM_INTERVAL_HOURS hours, Claude:
  1. Reads BTC price/RSI/z-score, Kalshi signals, positions, fills, and balance from Redis
  2. Returns a concise JSON policy document
  3. Writes each key to ep:config in Redis
  4. The trading bot reads overrides from ep:config on its next cycle

Prompt caching: the ~1 KB system prompt is sent with cache_control=ephemeral
so repeated runs bill only for the small (~200-token) context delta.

Usage:
  # One-shot (cron / systemd timer):
  ANTHROPIC_API_KEY=sk-... python3 llm_agent.py

  # Continuous loop (screen session):
  LLM_INTERVAL_HOURS=4 python3 llm_agent.py --loop
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ── Dependency check ──────────────────────────────────────────────────────────
try:
    import anthropic
except ImportError:
    print("[llm_agent] ERROR: anthropic not installed. Run: pip install anthropic", flush=True)
    sys.exit(1)

try:
    import redis.asyncio as aioredis
except ImportError:
    print("[llm_agent] ERROR: redis not installed. Run: pip install redis", flush=True)
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_MODEL   = os.getenv("LLM_MODEL",            "claude-opus-4-6")
RUN_INTERVAL_H = float(os.getenv("LLM_INTERVAL_HOURS", "4"))
REDIS_URL      = os.getenv("REDIS_URL",             "redis://localhost:6379/0")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY",     "")
MAX_TOKENS     = int(os.getenv("LLM_MAX_TOKENS",    "512"))

# Redis key namespace (mirrors ep_config — no circular import here)
EP_PRICES     = "ep:prices"
EP_BALANCE    = "ep:balance"
EP_POSITIONS  = "ep:positions"
EP_EXECUTIONS = "ep:executions"
EP_SYSTEM     = "ep:system"
EP_CONFIG     = "ep:config"


# ── System prompt (cached by Anthropic — ~1 KB, only billed on cache-miss) ───

_SYSTEM_PROMPT = """\
You are the EdgePulse policy engine — a concise, quantitative trading assistant.

You review live market context from a BTC + Kalshi prediction-market bot and
return a JSON policy document that the bot applies on its next cycle.

Output format (strict JSON, no prose, no markdown fences):
{
  "rsi_oversold":   <number 20–45>,
  "rsi_overbought": <number 55–80>,
  "z_threshold":    <number 0.5–3.0>,
  "kelly_fraction": <number 0.05–0.40>,
  "max_contracts":  <integer 1–20>,
  "btc_enabled":    <true|false>,
  "kalshi_enabled": <true|false>,
  "halt_trading":   <true|false>,
  "scale_factor":   <number 0.1–2.0>,
  "notes":          "<one-sentence rationale, under 120 chars>"
}

Policy rules:
- TIGHTEN signals (raise RSI bounds, raise z_threshold) when:
    recent PnL is negative, drawdown > 10%, BTC volatility is spiking (|z| > 2.5).
- LOOSEN signals (lower RSI bounds, lower z_threshold) when:
    recent PnL is strongly positive, calm low-vol markets.
- Set halt_trading=true ONLY for: systemic data errors, drawdown > 20%, or clear exchange outage.
- scale_factor adjusts position sizing relative to Kelly (0.5 = half size, 1.5 = 50% more).
- Never set kelly_fraction > 0.40 — reckless above this level.
- When uncertain or data is sparse, default to: scale_factor=0.7, RSI 30/70, z_threshold=1.8.
- Return ONLY valid JSON. No explanation, no prose, no markdown.
"""


# ── Redis context builder ─────────────────────────────────────────────────────

async def _gather_context(r: aioredis.Redis) -> Dict[str, Any]:
    """Pull all relevant state from Redis and return as a dict for Claude."""
    ctx: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # BTC spot + computed indicators (written by ep_btc.py via ep_intel.py)
    prices_raw = await r.hgetall(EP_PRICES)
    btc_raw    = prices_raw.get(b"BTC-USD") or prices_raw.get("BTC-USD")
    if btc_raw:
        try:
            btc = json.loads(btc_raw)
            ctx["btc_price"]   = btc.get("last_price") or btc.get("yes_price")
            ctx["btc_z_score"] = btc.get("btc_z_score")
            ctx["btc_rsi"]     = btc.get("btc_rsi")
        except Exception:
            pass
    ctx.setdefault("btc_price",   None)
    ctx.setdefault("btc_z_score", None)
    ctx.setdefault("btc_rsi",     None)

    # Balance (sum across all nodes published to ep:balance)
    balances_raw  = await r.hgetall(EP_BALANCE)
    balance_cents = 0
    for v in balances_raw.values():
        try:
            balance_cents += json.loads(v).get("balance_cents", 0)
        except Exception:
            pass
    ctx["balance_usd"] = round(balance_cents / 100, 2)

    # Open positions (capped at 10 for context size)
    positions_raw = await r.hgetall(EP_POSITIONS)
    positions     = {}
    for k, v in positions_raw.items():
        key = k.decode() if isinstance(k, bytes) else k
        try:
            positions[key] = json.loads(v)
        except Exception:
            pass
    ctx["open_positions"] = len(positions)
    ctx["positions"]      = [
        {
            "ticker":      t,
            "side":        p.get("side"),
            "contracts":   p.get("contracts"),
            "entry_cents": p.get("entry_cents"),
            "asset_class": p.get("asset_class", "kalshi"),
        }
        for t, p in list(positions.items())[:10]
    ]

    # Recent execution reports (fills vs rejects)
    execs_raw      = await r.xrevrange(EP_EXECUTIONS, count=50)
    fills          = 0
    rejects        = 0
    pnl_edge_sum   = 0.0
    for _, mapping in execs_raw:
        try:
            payload = mapping.get(b"payload") or mapping.get("payload")
            if not payload:
                continue
            rep = json.loads(payload)
            if rep.get("status") == "filled":
                fills       += 1
                pnl_edge_sum += float(rep.get("edge_captured", 0))
            elif rep.get("status") in ("rejected", "expired"):
                rejects += 1
        except Exception:
            pass
    ctx["recent_fills"]    = fills
    ctx["recent_rejects"]  = rejects
    ctx["recent_pnl_edge"] = round(pnl_edge_sum, 4)   # sum of edge deltas

    # Last 5 system lifecycle events
    events_raw = await r.xrevrange(EP_SYSTEM, count=10)
    events     = []
    for _, mapping in events_raw:
        try:
            payload = mapping.get(b"payload") or mapping.get("payload")
            if payload:
                ev = json.loads(payload)
                events.append({"type": ev.get("event_type"), "node": ev.get("node")})
        except Exception:
            pass
    ctx["recent_events"] = events[:5]

    return ctx


# ── Policy writer ─────────────────────────────────────────────────────────────

async def _write_policy(r: aioredis.Redis, policy: Dict[str, Any]) -> None:
    """
    Write each policy key to ep:config.
    Keys are prefixed with 'llm_' to distinguish from operator overrides,
    EXCEPT halt_trading which also writes the canonical HALT_TRADING flag.
    """
    mapping: Dict[str, str] = {}

    for k, v in policy.items():
        if k == "halt_trading":
            # Wire into the existing is_halted() check in ep_bus.py
            mapping["HALT_TRADING"] = "1" if v else "0"
            mapping["llm_halt_trading"] = "1" if v else "0"
        elif k == "notes":
            mapping["llm_notes"] = str(v)[:240]
        elif isinstance(v, bool):
            mapping[f"llm_{k}"] = "1" if v else "0"
        else:
            mapping[f"llm_{k}"] = str(v)

    mapping["llm_last_run_ts"] = str(int(time.time()))

    if mapping:
        await r.hset(EP_CONFIG, mapping=mapping)


# ── Core run function ─────────────────────────────────────────────────────────

async def run_once(
    client: anthropic.AsyncAnthropic,
    r:      aioredis.Redis,
) -> Optional[Dict[str, Any]]:
    """
    Gather Redis context → call Claude (with prompt caching) → write policy.
    Returns the parsed policy dict or None on failure.
    """
    ctx = await _gather_context(r)

    user_content = (
        "Current EdgePulse market state:\n"
        + json.dumps(ctx, indent=2)
        + "\n\nReturn the JSON policy document."
    )

    try:
        response = await client.messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = MAX_TOKENS,
            system     = [
                {
                    "type":          "text",
                    "text":          _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages = [{"role": "user", "content": user_content}],
        )
    except anthropic.APIError as exc:
        print(f"[llm_agent] Anthropic API error: {exc}", flush=True)
        return None

    raw = response.content[0].text.strip()

    # Strip markdown code fences if Claude added them despite instructions
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    try:
        policy = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[llm_agent] JSON parse error: {exc!r}  raw={raw[:300]!r}", flush=True)
        return None

    # Sanity-check required keys
    required = {
        "rsi_oversold", "rsi_overbought", "z_threshold",
        "kelly_fraction", "max_contracts",
        "btc_enabled", "kalshi_enabled", "halt_trading",
    }
    missing = required - set(policy.keys())
    if missing:
        print(f"[llm_agent] WARNING: policy missing keys: {missing}", flush=True)

    await _write_policy(r, policy)

    # Print summary
    usage       = response.usage
    cache_read  = getattr(usage, "cache_read_input_tokens",     0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    print(
        f"[llm_agent] Policy written  "
        f"halt={policy.get('halt_trading')}  "
        f"scale={policy.get('scale_factor', 1.0):.2f}  "
        f"btc={policy.get('btc_enabled')}  "
        f"notes={policy.get('notes', '')!r}",
        flush=True,
    )
    print(
        f"[llm_agent] Tokens  in={usage.input_tokens}  out={usage.output_tokens}"
        f"  cache_write={cache_write}  cache_read={cache_read}",
        flush=True,
    )
    return policy


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(loop: bool) -> None:
    if not ANTHROPIC_KEY:
        print("[llm_agent] ERROR: ANTHROPIC_API_KEY not set in .env. Exiting.", flush=True)
        sys.exit(1)

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)
    r      = await aioredis.from_url(
        REDIS_URL,
        encoding         = "utf-8",
        decode_responses = False,
        socket_connect_timeout = 5,
    )

    print(
        f"[llm_agent] Starting  model={CLAUDE_MODEL}  "
        f"interval={RUN_INTERVAL_H}h  loop={loop}",
        flush=True,
    )

    try:
        if loop:
            while True:
                t0 = time.monotonic()
                await run_once(client, r)
                elapsed = time.monotonic() - t0
                sleep_s = max(0.0, RUN_INTERVAL_H * 3600 - elapsed)
                print(
                    f"[llm_agent] Next run in {sleep_s / 3600:.2f}h "
                    f"({int(sleep_s)}s)",
                    flush=True,
                )
                await asyncio.sleep(sleep_s)
        else:
            await run_once(client, r)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("[llm_agent] Shutdown.", flush=True)
    finally:
        await r.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EdgePulse LLM policy agent")
    parser.add_argument(
        "--loop",
        action  = "store_true",
        help    = "Run repeatedly every LLM_INTERVAL_HOURS hours (default: one-shot)",
    )
    args = parser.parse_args()
    asyncio.run(main(loop=args.loop))
