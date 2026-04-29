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

try:
    from ep_pg_audit import init_audit_writer, stop_audit_writer, audit as _audit
    _AUDIT_AVAILABLE = True
except ImportError:
    _AUDIT_AVAILABLE = False

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
CLAUDE_MODEL   = os.getenv("LLM_MODEL",            "claude-haiku-4-5-20251001")
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

Context fields you receive:
- category_stats: per-signal-category fills/rejects/pnl/avg_confidence.
    avg_conf < 0.60 in a category → model producing weak signals, reduce scale_factor.
    If fomc fills dominate, that is normal — FOMC is the primary signal source.
- reject_breakdown: reject-reason prefix counts (RISK=risk gate, EXPIRED=stale signals,
    MEETING=no FOMC meeting within window, SERIES=series exposure cap hit).
    MEETING/SERIES rejects are NORMAL and expected — the FOMC strategy filters most
    markets by meeting date and caps exposure per rate series (KXFED). Do NOT reduce
    scale_factor or max_contracts due to high MEETING or SERIES reject counts alone.
    High EXPIRED → signals arrive stale, no action needed.
    High RISK (from risk gate, not SERIES) with negative PnL → reduce max_contracts.
- Kalshi fills: FOMC limit orders are placed at limit prices into illiquid markets.
    They may rest unfilled for days/weeks — this is NORMAL. zero fills + resting orders
    does NOT mean the strategy is failing. Check open_positions for resting orders.
- model_sources: which model generated the most fills (kalshi_implied+fred vs fred_anchor).
    fred_anchor = CME FedWatch was unavailable; treat those signals with lower weight.
- strategy_performance: resolved-trade win rates and P&L per model_source (ground truth).
    overall_win_rate is typically 10–20% — most profit comes from a few large resolution wins
    (YES bought at 3¢ resolves at 100¢, or NO bought at 97¢ resolves at 0¢).
    Do NOT interpret low win_rate alone as failure; use expectancy_cents and pnl_usd instead.
    expectancy_cents > 0 means the system earns money per trade on average — do not tighten.
    avg_pnl_cents per strategy is the key signal:
      < -15¢ AND trades >= 10 → strategy is losing money; reduce scale_factor or kelly_fraction.
      > +25¢ AND trades >= 10 → strategy is working well; you may raise kelly_fraction up to 0.35.
    avg_win_cents >> |avg_loss_cents| is the expected pattern (asymmetric payoff structure).
- btc_fear_greed: 0–100 (0=extreme fear, 100=extreme greed).
    > 75 and BTC PnL negative → consider btc_enabled=false or lower scale.
    < 25 and shorts profitable → fear-driven bottom; ease z_threshold slightly.
- btc_funding_rate: perpetual swap funding. > 0.0015 → crowded longs. < -0.0015 → crowded shorts.
- btc_enabled: disable ONLY if BTC exchange API is confirmed unavailable. Do NOT disable
    due to unfilled FOMC limit orders or Kalshi-only rejects.

Policy rules:
- TIGHTEN signals (raise RSI bounds, raise z_threshold) when:
    recent PnL is negative, drawdown > 10%, BTC volatility spiking (|z| > 2.5),
    OR any category shows avg_conf < 0.58 with negative PnL.
- LOOSEN signals (lower RSI bounds, lower z_threshold) when:
    recent PnL is strongly positive, calm low-vol markets, all categories avg_conf > 0.70.
- Set halt_trading=true ONLY for: systemic data errors, drawdown > 20%, or exchange outage.
- scale_factor adjusts position sizing relative to Kelly (0.5 = half size, 1.5 = 50% more).
- Default scale_factor=1.0. Reduce only if drawdown > 10% OR consistent negative edge fills.
- Never set kelly_fraction > 0.40 — reckless above this level.
- When uncertain or data is sparse, default to: scale_factor=1.0, RSI 30/70, z_threshold=1.8.
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
    # Skip entries older than 10 minutes — orphan entries from removed services
    # (e.g. exec node that no longer publishes) would otherwise inflate the sum.
    balances_raw  = await r.hgetall(EP_BALANCE)
    balance_cents = 0
    now_us = time.time() * 1_000_000
    for v in balances_raw.values():
        try:
            d = json.loads(v)
            if now_us - d.get("ts_us", 0) > 600 * 1_000_000:
                continue
            balance_cents += d.get("balance_cents", 0)
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

    # Full-portfolio summary — computed over ALL positions before the detail cap.
    # Gives the agent accurate exposure context even when position count > 10.
    _by_series:     Dict[str, int]   = {}
    _by_asset:      Dict[str, int]   = {}
    _by_side:       Dict[str, int]   = {}
    _total_exp      = 0
    for t, p in positions.items():
        series      = t.split("-")[0] if "-" in t else t
        asset       = p.get("asset_class", "kalshi")
        side        = p.get("side", "yes")
        contracts   = int(p.get("contracts") or 0)
        entry_cents = int(p.get("entry_cents") or 0)
        cost        = entry_cents if side == "yes" else (100 - entry_cents)
        _by_series[series]  = _by_series.get(series, 0) + 1
        _by_asset[asset]    = _by_asset.get(asset, 0) + 1
        _by_side[side]      = _by_side.get(side, 0) + 1
        _total_exp         += cost * contracts

    ctx["portfolio_summary"] = {
        "total_positions":    len(positions),
        "by_series":          dict(sorted(_by_series.items(), key=lambda x: -x[1])),
        "by_asset_class":     _by_asset,
        "by_side":            _by_side,
        "total_exposure_cents": _total_exp,
        "total_exposure_usd": round(_total_exp / 100, 2),
    }

    # Detail list capped at 10 to limit prompt size; summary above covers the rest.
    ctx["positions"] = [
        {
            "ticker":      t,
            "side":        p.get("side"),
            "contracts":   p.get("contracts"),
            "entry_cents": p.get("entry_cents"),
            "asset_class": p.get("asset_class", "kalshi"),
        }
        for t, p in list(positions.items())[:10]
    ]

    # Recent execution reports — break down by category, exit reason, and confidence
    execs_raw    = await r.xrevrange(EP_EXECUTIONS, count=100)
    fills        = 0
    rejects      = 0
    pnl_edge_sum = 0.0

    # Per-category stats: {category: {fills, rejects, pnl, conf_sum, conf_n}}
    cat_stats: Dict[str, Any] = {}
    # Exit reason breakdown: {reason_prefix: count}
    exit_reasons: Dict[str, int] = {}
    # Model source hits
    model_hits: Dict[str, int] = {}

    for _, mapping in execs_raw:
        try:
            payload = mapping.get(b"payload") or mapping.get("payload")
            if not payload:
                continue
            rep = json.loads(payload)
            ac   = rep.get("asset_class", "kalshi")
            cat  = rep.get("category", ac)
            stat = cat_stats.setdefault(cat, {
                "fills": 0, "rejects": 0, "pnl": 0.0, "conf_sum": 0.0, "conf_n": 0
            })

            if rep.get("status") == "filled":
                # Filter out synthesized exit reports. Exits at ep_exec.py:2493
                # (and similar paths) reuse `edge_captured` to store dollar realized
                # PnL, while entries store per-contract decimal edge. Summing them
                # together produces incoherent values that can swing the advisor's
                # `recent_pnl_edge` by thousands while real PnL is small. Entry
                # fills always carry signal_id and cost_cents>0; synthesized exits
                # leave both empty.
                _is_entry = bool(rep.get("signal_id")) and rep.get("cost_cents", 0) > 0
                if not _is_entry:
                    continue
                fills        += 1
                edge          = float(rep.get("edge_captured", 0))
                pnl_edge_sum += edge
                stat["fills"]    += 1
                stat["pnl"]      += edge
                conf = rep.get("confidence")
                if conf is not None:
                    stat["conf_sum"] += float(conf)
                    stat["conf_n"]   += 1
                # Track exit reasons
                exit_r = rep.get("reject_reason") or ""
                if not exit_r:
                    # Infer from edge sign: positive = exit win, negative = exit loss
                    # (real reason not stored on exit reports, only on entry rejects)
                    pass
                src = rep.get("model_source", "")
                if src:
                    model_hits[src] = model_hits.get(src, 0) + 1
            elif rep.get("status") in ("rejected", "expired"):
                rejects     += 1
                stat["rejects"] += 1
                reason = rep.get("reject_reason", "other")
                # Group by prefix (RISK_GATE_*, EXPIRED, DUPLICATE, LLM_*)
                prefix = reason.split("_")[0] if "_" in reason else reason
                exit_reasons[prefix] = exit_reasons.get(prefix, 0) + 1
        except Exception:
            pass

    ctx["recent_fills"]    = fills
    ctx["recent_rejects"]  = rejects
    ctx["recent_pnl_edge"] = round(pnl_edge_sum, 4)

    # Summarise per-category stats with avg confidence
    cat_summary = {}
    for cat, s in cat_stats.items():
        avg_conf = round(s["conf_sum"] / s["conf_n"], 3) if s["conf_n"] > 0 else None
        cat_summary[cat] = {
            "fills":    s["fills"],
            "rejects":  s["rejects"],
            "pnl":      round(s["pnl"], 4),
            "avg_conf": avg_conf,
        }
    ctx["category_stats"]  = cat_summary
    ctx["reject_breakdown"] = exit_reasons
    ctx["model_sources"]   = dict(sorted(model_hits.items(), key=lambda x: -x[1])[:5])

    # BTC sentiment (fear/greed + funding rate, published by ep_btc.py into ep:prices)
    btc_full_raw = prices_raw.get(b"BTC-USD") or prices_raw.get("BTC-USD")
    if btc_full_raw:
        try:
            btc_full = json.loads(btc_full_raw)
            fg = btc_full.get("fear_greed")
            fr = btc_full.get("funding_rate")
            if fg is not None:
                ctx["btc_fear_greed"]   = fg
            if fr is not None:
                ctx["btc_funding_rate"] = fr
        except Exception:
            pass
    ctx.setdefault("btc_fear_greed",   None)
    ctx.setdefault("btc_funding_rate", None)

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

    # Historical strategy performance from ep:performance (written hourly by exec).
    # Gives the agent ground-truth win rates and P&L per model_source from resolved
    # trades — the Redis execution stream only covers recent entries/rejects and has
    # no outcome data.  Skip strategies with < 3 trades to suppress noise.
    raw_perf = await r.get("ep:performance")
    if raw_perf:
        try:
            perf     = json.loads(raw_perf)
            by_strat = {}
            for strat, s in perf.get("by_strategy", {}).items():
                trades = s.get("trades", 0)
                if trades < 3:
                    continue
                wins = s.get("wins", 0)
                pnl  = s.get("pnl_cents", 0)
                by_strat[strat] = {
                    "trades":        trades,
                    "win_rate":      round(wins / trades, 3) if trades else 0.0,
                    "pnl_usd":       round(pnl / 100, 2),
                    "avg_pnl_cents": round(pnl / trades, 1) if trades else 0.0,
                }
            ctx["strategy_performance"] = {
                "period_days":      perf.get("period_days"),
                "total_trades":     perf.get("total_trades"),
                "overall_win_rate": perf.get("win_rate"),
                "total_pnl_usd":    round(perf.get("total_pnl_cents", 0) / 100, 2),
                "expectancy_cents": perf.get("expectancy_cents"),
                "avg_win_cents":    perf.get("avg_win_cents"),
                "avg_loss_cents":   perf.get("avg_loss_cents"),
                "avg_hold_hours":   perf.get("avg_hold_time_hours"),
                "by_strategy":      dict(sorted(
                    by_strat.items(), key=lambda x: -x[1]["pnl_usd"]
                )),
            }
        except Exception:
            pass

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

    # Snapshot current policy before overwriting it
    try:
        raw_before = await r.hgetall(EP_CONFIG)
        config_before = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw_before.items()
        }
    except Exception:
        config_before = {}

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

    if not response.content or not getattr(response.content[0], "text", None):
        print("[llm_agent] ERROR: empty response content", flush=True)
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
        print(f"[llm_agent] ERROR: policy missing required keys: {missing} — skipping write", flush=True)
        return None

    await _write_policy(r, policy)

    if _AUDIT_AVAILABLE:
        try:
            _audit().write("llm_decisions", {
                "ts_us":              int(time.time() * 1_000_000),
                "model":              CLAUDE_MODEL,
                "prompt_tokens":      response.usage.input_tokens,
                "completion_tokens":  response.usage.output_tokens,
                "config_before":      config_before,
                "config_after":       policy,
                "reasoning":          policy.get("notes", ""),
            })
        except Exception:
            pass

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

    if _AUDIT_AVAILABLE:
        await init_audit_writer()

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
        if _AUDIT_AVAILABLE:
            await stop_audit_writer()
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
