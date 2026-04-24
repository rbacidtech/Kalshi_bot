"""
ep_advisor.py — LLM performance monitor and operator alert service.

Separate from llm_agent.py (policy engine).  This service:
  • Detects strategy drift by comparing recent vs baseline win rates
  • Flags concentration risk in portfolio exposure
  • Emits structured alerts to ep:alerts Redis stream
  • Auto-applies one whitelisted ep:config adjustment per run (confidence ≥ 0.80)
  • Fires Telegram on critical alerts
  • Escalates from Haiku → Sonnet when degradation or risk is detected

Runs every 30 minutes on the Exec node (shares TRADES_CSV access with ep_exec.py).
Kill switch: ADVISOR_DISABLED=1 in .env.

Usage:
  python3 ep_advisor.py          # one-shot
  python3 ep_advisor.py --loop   # continuous (respects ADVISOR_INTERVAL_S)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

try:
    import anthropic
except ImportError:
    print("[ep_advisor] ERROR: anthropic not installed. Run: pip install anthropic", flush=True)
    sys.exit(1)

try:
    import redis.asyncio as aioredis
except ImportError:
    print("[ep_advisor] ERROR: redis not installed. Run: pip install redis", flush=True)
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
HAIKU_MODEL         = os.getenv("ADVISOR_MODEL",          "claude-haiku-4-5-20251001")
SONNET_MODEL        = os.getenv("ADVISOR_ESCALATE_MODEL", "claude-sonnet-4-6")
RUN_INTERVAL_S      = int(os.getenv("ADVISOR_INTERVAL_S", "1800"))   # 30 min
ADVISOR_DISABLED    = os.getenv("ADVISOR_DISABLED", "0") == "1"
REDIS_URL           = os.getenv("REDIS_URL",              "redis://localhost:6379/0")
ANTHROPIC_KEY       = os.getenv("ANTHROPIC_API_KEY",      "")
MAX_TOKENS_HAIKU    = int(os.getenv("ADVISOR_MAX_TOKENS",          "800"))
MAX_TOKENS_SONNET   = int(os.getenv("ADVISOR_MAX_TOKENS_ESCALATED", "1400"))

EP_ALERTS              = "ep:alerts"
EP_CONFIG              = "ep:config"
EP_POSITIONS           = "ep:positions"
EP_BALANCE             = "ep:balance"
EP_ADVISOR_STATUS      = "ep:advisor:status"
EP_SPREAD_WIDE_SINCE   = "ep:advisor:spread_wide_since"

# Datasource keys with their configured TTLs (mirrors ep_datasources._SOURCES)
_DS_SOURCES: list = [
    ("ep:sofr:sr1",           "sofr_sr1",           300),
    ("ep:sofr:sr3",           "sofr_sr3",            300),
    ("ep:treasury_auctions",  "treasury_auctions",  86400),
    ("ep:econ_consensus",     "econ_consensus",      3600),
    ("ep:deribit:skew",       "deribit_skew",         600),
    ("ep:btc:cross_exchange", "btc_cross_exchange",   120),
    ("ep:macro:walcl",        "walcl",              86400),
    ("ep:macro:baa10y",       "baa10y",              3600),
    ("ep:predictit:markets",  "predictit_markets",    300),
]

# Whitelisted ep:config keys the advisor may auto-apply.
# Value: (min, max) for numeric bounds, or None for special handling.
# (lo, hi, max_delta_per_cycle). max_delta caps how far Claude can move a
# value in a single 30-min advisor cycle. Absolute bounds were present since
# inception; the per-cycle cap was added 2026-04-24 after a CRITICAL audit
# finding that a single hallucinated adjustment could swing any whitelisted
# key across its full range (e.g., llm_kelly_fraction 0.05 → 0.35 = 7×
# position sizing in one tick).
_WHITELIST: Dict[str, Optional[Tuple[float, float, float]]] = {
    "llm_scale_factor":   (0.10, 2.00, 0.30),
    "llm_kelly_fraction": (0.05, 0.35, 0.05),
    "llm_rsi_oversold":   (20.0, 45.0, 5.0),
    "llm_rsi_overbought": (55.0, 80.0, 5.0),
    "llm_z_threshold":    (0.50, 3.00, 0.50),
    "llm_max_contracts":  (1.0,  20.0, 3.0),
    "HALT_TRADING":       None,   # special: "1" only, confidence ≥ 0.95
}

_SYSTEM_PROMPT = """\
You are the EdgePulse Advisor — a performance monitoring agent separate from the baseline policy engine.

Your job: analyse recent strategy data and emit operator alerts. You look for drift, degradation, and concentration risk.

Output format (strict JSON, no prose, no markdown fences):
{
  "alerts": [
    {
      "severity": "info|warning|critical",
      "category": "strategy_health|concentration|pnl|system",
      "title": "<≤60 chars>",
      "message": "<specific, numbers-backed ≤200 chars>",
      "recommended_action": "<what to do, or null>"
    }
  ],
  "adjustment": {
    "key":        "<whitelisted key>",
    "value":      "<string value>",
    "confidence": <0.0–1.0>,
    "rationale":  "<≤120 chars with specific numbers>"
  },
  "summary": "<one sentence ≤120 chars>",
  "severity_overall": "info|warning|critical"
}

If no confident adjustment: set "adjustment" to null.

Severity thresholds:
- critical: any strategy degrading AND 7d P&L < -$50, OR any category > 80% exposure
- warning:  any strategy degrading, OR any category > 60% exposure, OR 7d P&L < -$50
- info:     improvements, stable state, routine observations

Adjustment rules:
- Suggest at most ONE adjustment (highest-confidence change only)
- Whitelisted keys: llm_scale_factor, llm_kelly_fraction, HALT_TRADING,
  llm_rsi_oversold, llm_rsi_overbought, llm_z_threshold, llm_max_contracts
- confidence < 0.80 → set adjustment to null (operator decides)
- HALT_TRADING="1" requires confidence ≥ 0.95; never suggest "0" (only operators un-halt)
- Reduce llm_kelly_fraction only when a strategy has ≥ 5 recent trades AND is degrading
- Never suggest llm_kelly_fraction > 0.35 or llm_scale_factor > 1.5

Strategy health interpretation:
- "degrading": recent win rate dropped ≥ 10pp vs baseline — notable but check trade count
- "insufficient_data" → do not act on this strategy
- FOMC win rates are typically 10-25% (large asymmetric payoffs); 0% recent ≠ broken
- avg_pnl_cents is more reliable than win_rate for FOMC strategies
- fomc > 60% of total exposure is NORMAL for this bot; do NOT raise critical for this alone

Data quality (data_quality field in context):
- Each source has status: "ok", "stale", "missing", or "error"
- sofr_sr1/sofr_sr3 stale/missing → reduced FOMC probability fusion reliability
- btc_cross_exchange stale → BTC spread gate may be inactive; flag if recent trades present
- Raise warning (not critical) for stale data unless combined with degrading performance
- Do not raise critical for data staleness alone

Emit at most 5 alerts. Prioritize by severity. Return only valid JSON.
"""


# ── Datasource staleness checker ─────────────────────────────────────────────

async def _check_datasource_staleness(r: aioredis.Redis) -> Dict[str, Any]:
    """
    Return per-source freshness by checking Redis TTL against configured TTL.
    Uses remaining TTL (key expiry) as proxy for freshness — if key is absent,
    source has not refreshed within its configured window.
    """
    result: Dict[str, Any] = {}

    async def _check_one(redis_key: str, name: str, ttl_s: int) -> None:
        try:
            remaining = await r.ttl(redis_key)   # -2 = absent, -1 = no TTL, ≥0 = seconds left
            if remaining == -2:
                result[name] = {"status": "missing", "age_s": None, "ttl_s": ttl_s}
            else:
                age_s = max(0, ttl_s - remaining) if remaining >= 0 else None
                stale = remaining >= 0 and remaining < ttl_s * 0.10   # < 10% TTL left
                result[name] = {
                    "status":  "stale" if stale else "ok",
                    "age_s":   age_s,
                    "ttl_s":   ttl_s,
                }
        except Exception as exc:
            result[name] = {"status": "error", "error": str(exc)[:80]}

    await asyncio.gather(*[_check_one(k, n, t) for k, n, t in _DS_SOURCES])
    return result


async def _update_spread_alert(r: aioredis.Redis) -> Optional[str]:
    """
    Track whether BTC cross-exchange spread has been persistently > 15 bps.
    Returns a critical alert message if sustained ≥ 10 minutes, else None.
    """
    try:
        cx_raw = await r.get("ep:btc:cross_exchange")
        if not cx_raw:
            await r.delete(EP_SPREAD_WIDE_SINCE)
            return None
        cx_data    = json.loads(cx_raw)
        spread_bps = float(cx_data.get("spread_bps", 0))
        if spread_bps > 15.0:
            existing = await r.get(EP_SPREAD_WIDE_SINCE)
            if not existing:
                await r.set(EP_SPREAD_WIDE_SINCE, str(time.time()), ex=3600)
                return None   # just started — not yet sustained
            wide_s = time.time() - float(existing)
            if wide_s >= 600:
                return (
                    f"BTC cross-exchange spread {spread_bps:.1f} bps wide "
                    f"for {wide_s / 60:.1f} min — "
                    "fragmented market, mean-reversion signals suppressed"
                )
        else:
            await r.delete(EP_SPREAD_WIDE_SINCE)
    except Exception:
        pass
    return None


# ── Context builder ───────────────────────────────────────────────────────────

async def _gather_context(r: aioredis.Redis) -> Dict[str, Any]:
    """Read all relevant state from Redis and compute advisor context."""
    from ep_resolution_db import (
        get_rolling_strategy_health,
        get_concentration_metrics,
        get_kelly_by_strategy,
        get_performance_summary,
        compute_yes_entry_price_gate,
        compute_near_expiry_stop_days,
    )

    ctx: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # ── Positions + balance ──────────────────────────────────────────────────
    pos_raw, bal_raw = await asyncio.gather(
        r.hgetall(EP_POSITIONS),
        r.hgetall(EP_BALANCE),
    )
    positions: Dict[str, dict] = {}
    for k, v in (pos_raw or {}).items():
        key = k.decode() if isinstance(k, bytes) else k
        try:
            positions[key] = json.loads(v)
        except Exception:
            pass

    balance_cents = 0
    for v in (bal_raw or {}).values():
        try:
            balance_cents += json.loads(v).get("balance_cents", 0)
        except Exception:
            pass
    ctx["balance_usd"] = round(balance_cents / 100, 2)

    # ── Performance (7d and 30d) ─────────────────────────────────────────────
    perf_7d, perf_30d = await asyncio.gather(
        get_performance_summary(days=7),
        get_performance_summary(days=30),
    )
    ctx["performance_7d"] = {
        "total_trades":    perf_7d.get("total_trades"),
        "win_rate":        perf_7d.get("win_rate"),
        "total_pnl_cents": perf_7d.get("total_pnl_cents"),
        "expectancy_cents":perf_7d.get("expectancy_cents"),
        "avg_win_cents":   perf_7d.get("avg_win_cents"),
        "avg_loss_cents":  perf_7d.get("avg_loss_cents"),
        "by_strategy":     perf_7d.get("by_strategy"),
    }
    ctx["performance_30d"] = {
        "total_trades":    perf_30d.get("total_trades"),
        "win_rate":        perf_30d.get("win_rate"),
        "total_pnl_cents": perf_30d.get("total_pnl_cents"),
        "expectancy_cents":perf_30d.get("expectancy_cents"),
    }

    # ── Strategy health (CSV rolling window) ────────────────────────────────
    ctx["strategy_health"] = await get_rolling_strategy_health(recent_n=20, baseline_n=50)

    # ── Portfolio concentration ──────────────────────────────────────────────
    ctx["concentration"] = get_concentration_metrics(positions)

    # ── Kelly deployment by strategy ─────────────────────────────────────────
    ctx["kelly_by_strategy"] = get_kelly_by_strategy(positions, balance_cents)
    ctx["open_positions"] = len(positions)

    # ── Resolution-DB threshold calibration ─────────────────────────────────
    # Compute data-driven YES price gate and near-expiry stop window.
    # Write calibrated values to ep:config so Intel + Exec pick them up live.
    try:
        yes_gate  = compute_yes_entry_price_gate()
        stop_days = compute_near_expiry_stop_days()
        ctx["threshold_calibration"] = {
            "yes_entry_price_gate": yes_gate,
            "near_expiry_stop_days": stop_days,
        }
        # Auto-apply if calibration has enough data and differs from defaults.
        # When used_default=True, surface the reason so operators can tell
        # whether it's data insufficiency, out-of-range, or a scanner being dead.
        if not yes_gate["used_default"]:
            calibrated_str = f"{yes_gate['calibrated']:.2f}"
            await r.hset(EP_CONFIG, "override_min_yes_entry_price", calibrated_str)
        else:
            print(
                f"[ep_advisor] yes_entry_gate used default "
                f"({yes_gate.get('default')}): {yes_gate.get('note', 'no reason given')}",
                flush=True,
            )
        if not stop_days["used_default"]:
            await r.hset(EP_CONFIG, "kalshi_near_expiry_no_stop_days", str(stop_days["calibrated"]))
        else:
            print(
                f"[ep_advisor] near_expiry_stop_days used default "
                f"({stop_days.get('default')}): {stop_days.get('note', 'no reason given')}",
                flush=True,
            )
    except Exception as _cal_exc:
        ctx["threshold_calibration"] = {"error": str(_cal_exc)[:120]}

    # ── Current ep:config overrides (so Claude knows what's already set) ─────
    try:
        cfg_raw = await r.hgetall(EP_CONFIG)
        # Normalize bytes-keyed hashes to str keys up-front so downstream
        # lookups don't need the brittle `get(k.encode(), get(k, None))`
        # fallback. aioredis returns bytes when decode_responses=False and
        # str when True; we want a single shape either way.
        cfg_norm = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in cfg_raw.items()
        }
        cfg_keys = [
            "llm_scale_factor", "llm_kelly_fraction", "llm_rsi_oversold",
            "llm_rsi_overbought", "llm_z_threshold", "llm_max_contracts",
            "HALT_TRADING", "llm_notes",
        ]
        ctx["current_config"] = {k: cfg_norm.get(k) for k in cfg_keys}
    except Exception:
        ctx["current_config"] = {}

    # ── Datasource freshness ─────────────────────────────────────────────────
    try:
        ctx["data_quality"] = await _check_datasource_staleness(r)
    except Exception:
        ctx["data_quality"] = {}

    return ctx


# ── Escalation check ──────────────────────────────────────────────────────────

def _check_escalation(ctx: Dict[str, Any]) -> list:
    """Return list of escalation reason strings; empty = use Haiku."""
    reasons = []
    health = ctx.get("strategy_health", {})
    for strat, h in health.items():
        if h.get("status") == "degrading" and h.get("recent_n", 0) >= 5:
            reasons.append(f"degrading:{strat}")

    max_pct = ctx.get("concentration", {}).get("max_category_pct", 0.0)
    if max_pct > 0.60:
        name = ctx.get("concentration", {}).get("max_category_name", "?")
        reasons.append(f"concentration:{name}:{max_pct:.0%}")

    pnl_7d = ctx.get("performance_7d", {}).get("total_pnl_cents", 0) or 0
    if pnl_7d < -5000:
        reasons.append(f"pnl_7d:{pnl_7d/100:.2f}")

    return reasons


# ── Auto-apply safety ─────────────────────────────────────────────────────────

def _validate_adjustment(adj: dict, current: Optional[dict] = None) -> Optional[str]:
    """
    Validate an adjustment dict from Claude.
    Returns an error string if invalid, or None if it passes all checks.

    `current`: optional dict of current ep:config values (bytes or str).
    When provided, enforces the per-cycle max_delta clamp.
    """
    key   = adj.get("key", "")
    value = str(adj.get("value", "")).strip()
    conf  = float(adj.get("confidence", 0.0))

    if key not in _WHITELIST:
        return f"key {key!r} not in whitelist"

    if key == "HALT_TRADING":
        if value != "1":
            return "HALT_TRADING auto-apply only allows value='1' (never auto-un-halt)"
        if conf < 0.95:
            return f"HALT_TRADING requires confidence ≥ 0.95 (got {conf:.2f})"
        return None

    if conf < 0.80:
        return f"confidence {conf:.2f} < 0.80 threshold"

    bounds = _WHITELIST[key]
    if bounds:
        try:
            v = float(value)
        except (ValueError, TypeError):
            return f"value {value!r} is not numeric"
        lo, hi, max_delta = bounds
        if not (lo <= v <= hi):
            return f"value {v} outside bounds [{lo}, {hi}]"

        # Per-cycle delta clamp — prevents a single hallucinated adjustment from
        # swinging a whitelisted key across its full range in one tick.
        if current is not None:
            cur_raw = current.get(key)
            if cur_raw is None:
                # Also try bytes key for dicts read from aioredis raw hgetall
                cur_raw = current.get(key.encode()) if isinstance(current, dict) else None
            if cur_raw is not None:
                if isinstance(cur_raw, bytes):
                    cur_raw = cur_raw.decode()
                try:
                    cur_v = float(cur_raw)
                    delta = abs(v - cur_v)
                    if delta > max_delta:
                        return (
                            f"delta {delta:.3f} (current={cur_v} → new={v}) "
                            f"exceeds max_delta {max_delta} for one cycle"
                        )
                except (ValueError, TypeError):
                    pass  # current value not numeric; skip delta check

    return None


# ── Alert emitter ─────────────────────────────────────────────────────────────

async def _emit_alert(
    r:            aioredis.Redis,
    severity:     str,
    category:     str,
    title:        str,
    message:      str,
    action:       Optional[str] = None,
    auto_applied: bool          = False,
) -> None:
    payload = json.dumps({
        "ts":           datetime.now(timezone.utc).isoformat(),
        "severity":     severity,
        "category":     category,
        "title":        title,
        "message":      message,
        "action":       action,
        "auto_applied": auto_applied,
    })
    await r.xadd(EP_ALERTS, {"payload": payload}, maxlen=500, approximate=True)


# ── Core run ──────────────────────────────────────────────────────────────────

async def run_once(
    client: anthropic.AsyncAnthropic,
    r:      aioredis.Redis,
) -> Optional[Dict[str, Any]]:
    """One advisor cycle: gather context → Claude → emit alerts → auto-apply."""
    t0 = time.monotonic()

    ctx              = await _gather_context(r)

    # Pre-LLM: spread alert doesn't need Claude — emit immediately if triggered
    _spread_msg = await _update_spread_alert(r)
    if _spread_msg:
        await _emit_alert(
            r,
            severity  = "critical",
            category  = "system",
            title     = "BTC cross-exchange spread sustained wide",
            message   = _spread_msg,
            action    = "Mean-reversion signals auto-suppressed by ep_intel.py",
        )
        try:
            from ep_telegram import telegram
            await telegram.send_alert(f"[Advisor CRITICAL] {_spread_msg}", level="critical")
        except Exception:
            pass

    escalation_reasons = _check_escalation(ctx)
    model_used       = SONNET_MODEL if escalation_reasons else HAIKU_MODEL

    user_message = (
        "Current EdgePulse state:\n"
        + json.dumps(ctx, indent=2, default=str)
        + "\n\nReturn the JSON advisor report."
    )

    print(
        f"[ep_advisor] Running  model={model_used}  "
        f"positions={ctx.get('open_positions')}  "
        f"escalated={bool(escalation_reasons)}",
        flush=True,
    )

    max_tokens = MAX_TOKENS_SONNET if escalation_reasons else MAX_TOKENS_HAIKU

    try:
        response = await client.messages.create(
            model      = model_used,
            max_tokens = max_tokens,
            system     = [
                {
                    "type":          "text",
                    "text":          _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages = [{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as exc:
        print(f"[ep_advisor] Anthropic API error: {exc}", flush=True)
        return None

    if not response.content or not getattr(response.content[0], "text", None):
        print("[ep_advisor] Empty response from Claude", flush=True)
        return None

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[ep_advisor] JSON parse error: {exc!r}  raw={raw[:300]!r}", flush=True)
        return None

    alerts        = report.get("alerts", [])
    adjustment    = report.get("adjustment")
    summary       = report.get("summary", "")
    sev_overall   = report.get("severity_overall", "info")

    # ── Emit alerts to ep:alerts stream ─────────────────────────────────────
    alerts_emitted = 0
    for a in alerts[:5]:   # hard cap at 5
        try:
            await _emit_alert(
                r,
                severity  = a.get("severity", "info"),
                category  = a.get("category", "system"),
                title     = str(a.get("title", ""))[:80],
                message   = str(a.get("message", ""))[:240],
                action    = a.get("recommended_action"),
                auto_applied = False,
            )
            alerts_emitted += 1
        except Exception as exc:
            print(f"[ep_advisor] emit_alert error: {exc}", flush=True)

    # ── Telegram for critical alerts ─────────────────────────────────────────
    if sev_overall == "critical":
        try:
            from ep_telegram import telegram
            msg = f"[Advisor CRITICAL] {summary}"
            if alerts:
                top = alerts[0]
                msg = f"[Advisor CRITICAL] {top.get('title', '')}: {top.get('message', '')}"
            await telegram.send_alert(msg, level="critical")
        except Exception as exc:
            print(f"[ep_advisor] Telegram error: {exc}", flush=True)

    # ── Auto-apply at most one whitelisted adjustment ─────────────────────────
    applied: Optional[dict] = None
    if adjustment and isinstance(adjustment, dict):
        err = _validate_adjustment(adjustment, ctx.get("current_config"))
        if err:
            print(f"[ep_advisor] Adjustment rejected: {err}", flush=True)
            await _emit_alert(
                r,
                severity     = "info",
                category     = "config_applied",
                title        = "Adjustment rejected",
                message      = f"{adjustment.get('key')} → {adjustment.get('value')}: {err}",
                action       = "No change made",
                auto_applied = False,
            )
        else:
            key   = adjustment["key"]
            value = str(adjustment["value"]).strip()
            try:
                await r.hset(EP_CONFIG, key, value)
                applied = {"key": key, "value": value}
                print(
                    f"[ep_advisor] Auto-applied  {key}={value}  "
                    f"confidence={adjustment.get('confidence', 0):.2f}",
                    flush=True,
                )
                await _emit_alert(
                    r,
                    severity     = "info",
                    category     = "config_applied",
                    title        = f"Config auto-applied: {key}={value}",
                    message      = str(adjustment.get("rationale", ""))[:200],
                    action       = f"Wrote {key}={value} to ep:config",
                    auto_applied = True,
                )
                alerts_emitted += 1
            except Exception as exc:
                print(f"[ep_advisor] Redis write error: {exc}", flush=True)

    # ── Write status snapshot to ep:advisor:status ───────────────────────────
    elapsed = round(time.monotonic() - t0, 2)
    status_payload = json.dumps({
        "last_run_ts":       datetime.now(timezone.utc).isoformat(),
        "model_used":        model_used,
        "escalated":         bool(escalation_reasons),
        "escalation_reasons": escalation_reasons,
        "strategy_health":   ctx.get("strategy_health", {}),
        "concentration":     ctx.get("concentration", {}),
        "kelly_by_strategy": ctx.get("kelly_by_strategy", {}),
        "performance_7d":    ctx.get("performance_7d", {}),
        "alerts_emitted":    alerts_emitted,
        "auto_applied":      applied,
        "summary":           summary,
        "severity_overall":  sev_overall,
        "run_duration_s":    elapsed,
    }, default=str)

    try:
        await r.set(EP_ADVISOR_STATUS, status_payload, ex=7200)   # TTL 2h
    except Exception as exc:
        print(f"[ep_advisor] Status write error: {exc}", flush=True)

    usage       = response.usage
    cache_read  = getattr(usage, "cache_read_input_tokens",     0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    print(
        f"[ep_advisor] Done  severity={sev_overall}  alerts={alerts_emitted}  "
        f"applied={applied}  elapsed={elapsed}s  "
        f"tokens in={usage.input_tokens} out={usage.output_tokens} "
        f"cache_write={cache_write} cache_read={cache_read}",
        flush=True,
    )
    return report


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(loop: bool) -> None:
    if ADVISOR_DISABLED:
        print("[ep_advisor] Disabled via ADVISOR_DISABLED=1 — exiting.", flush=True)
        return

    if not ANTHROPIC_KEY:
        print("[ep_advisor] ERROR: ANTHROPIC_API_KEY not set. Exiting.", flush=True)
        sys.exit(1)

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)
    r      = await aioredis.from_url(
        REDIS_URL,
        encoding               = "utf-8",
        decode_responses       = False,
        socket_connect_timeout = 5,
    )

    print(
        f"[ep_advisor] Starting  haiku={HAIKU_MODEL}  sonnet={SONNET_MODEL}  "
        f"interval={RUN_INTERVAL_S}s  loop={loop}",
        flush=True,
    )

    try:
        if loop:
            while True:
                t0 = time.monotonic()
                try:
                    await run_once(client, r)
                except Exception as exc:
                    print(f"[ep_advisor] run_once error: {exc}", flush=True)
                elapsed  = time.monotonic() - t0
                sleep_s  = max(0.0, RUN_INTERVAL_S - elapsed)
                print(f"[ep_advisor] Next run in {sleep_s:.0f}s", flush=True)
                await asyncio.sleep(sleep_s)
        else:
            await run_once(client, r)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("[ep_advisor] Shutdown.", flush=True)
    finally:
        await r.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EdgePulse LLM advisor service")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously every ADVISOR_INTERVAL_S seconds")
    args = parser.parse_args()
    asyncio.run(main(loop=args.loop))
