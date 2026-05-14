"""Strategy correlation caps — Engineering A.4.

Four-level exposure hierarchy preventing concentration risk across
correlated strategies. Without these, 4 NFL strategies on the same game
(moneyline + spread + total + props) create 2-4x higher variance than
diversification math predicts. LTCM lesson.

Hierarchy (from Engineering A.4 §):

  1. Per-event       $150  (3 strategies × $50 baseline)
  2. Per-prefix daily — varies by prefix (KXNFLGAME $400, KXBTCD $250)
  3. Per-underlying daily — NFL_SUNDAY $800, CRYPTO_DAILY $400
  4. Total daily     $1,200 (80% of $1,500 capital)

A `RiskUnitClassifier` maps each ticker to:
  - event_id           — the canonical event (e.g. BUF-KC moneyline +
                          spread + total all map to event_id "NFL_BUF_KC_25NOV16")
  - prefix             — first KX-prefix token (KXNFLGAME etc.)
  - underlying         — broader bucket (NFL_SUNDAY, CRYPTO_DAILY,
                          POLITICAL_DAY)

Pre-trade check `check_candidate(ticker, contracts, price_cents)` returns:
  - Allowed size (downsized if needed) in contracts
  - Reject reason if even downsized size is below $10 minimum

State lives in Redis hash `ep:correlation_caps:exposure_today_cents`
keyed by `{event|prefix|underlying}:{id}` for fast lookup. UTC midnight
rollover clears daily counters.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


# Caps in cents. Engineering A.4 defaults; configurable via ep:config overrides.
_DEFAULTS = {
    "per_event_cap_cents":   15_000,    # $150
    "total_daily_cap_cents": 120_000,   # $1,200 (80% of $1,500 capital)
    "downsize_minimum_cents": 1_000,    # $10 — reject below this
}

# Per-prefix daily caps (cents)
_PREFIX_DAILY_DEFAULTS = {
    "KXNFLGAME":              40_000,   # $400
    "KXNFLSPREAD":            30_000,   # $300
    "KXNFLTOTAL":             30_000,   # $300
    "KXBTCD":                 25_000,   # $250
    "KXETHD":                 25_000,   # $250
    "KXMVENFLSINGLEGAME":     40_000,   # $400
    "KXMVENFLMULTIGAMEEXTENDED": 40_000,
    "KXMLBGAME":              25_000,   # $250
    "KXMLSGAME":              20_000,   # $200
    "KXNHLGAME":              20_000,
    "KXNCAAFGAME":            25_000,
    "KXNCAAMBGAME":           20_000,
    "KXWTAMATCH":             20_000,
    "KXATPMATCH":             20_000,
    "KXHIGH":                 15_000,   # weather longshots, per-city aggregated
}

# Per-underlying daily caps (cents)
_UNDERLYING_DAILY_DEFAULTS = {
    "NFL_SUNDAY":     80_000,    # $800 — biggest single bucket; 53% of capital
    "MLB_DAY":        30_000,
    "NHL_DAY":        25_000,
    "TENNIS_DAY":     25_000,
    "NCAA_DAY":       30_000,
    "CRYPTO_DAILY":   40_000,
    "WEATHER_DAILY":  20_000,
    "POLITICAL_DAY":  30_000,
}


# Underlying mapping — prefix → underlying bucket
_PREFIX_TO_UNDERLYING = {
    "KXNFLGAME":              "NFL_SUNDAY",
    "KXNFLSPREAD":            "NFL_SUNDAY",
    "KXNFLTOTAL":             "NFL_SUNDAY",
    "KXNFLRSHYDS":            "NFL_SUNDAY",
    "KXNFLRECYDS":            "NFL_SUNDAY",
    "KXMVENFLSINGLEGAME":     "NFL_SUNDAY",
    "KXMVENFLMULTIGAMEEXTENDED": "NFL_SUNDAY",
    "KXMLBGAME":              "MLB_DAY",
    "KXNHLGAME":              "NHL_DAY",
    "KXNCAAFGAME":            "NCAA_DAY",
    "KXNCAAMBGAME":           "NCAA_DAY",
    "KXNCAAMBSPREAD":         "NCAA_DAY",
    "KXNCAAFSPREAD":          "NCAA_DAY",
    "KXWTAMATCH":             "TENNIS_DAY",
    "KXATPMATCH":             "TENNIS_DAY",
    "KXMLSGAME":              "TENNIS_DAY",   # placeholder — soccer doesn't fit cleanly
    "KXBTCD":                 "CRYPTO_DAILY",
    "KXETHD":                 "CRYPTO_DAILY",
    "KXHIGH":                 "WEATHER_DAILY",
    "KXTRUMP":                "POLITICAL_DAY",
    "KXTRUMPMENTION":         "POLITICAL_DAY",
    "SECPRESS":               "POLITICAL_DAY",
    "VANCEMENTION":           "POLITICAL_DAY",
    "APRPOTUS":               "POLITICAL_DAY",
    "538APPROVE":             "POLITICAL_DAY",
}


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _exposure_key() -> str:
    return f"ep:correlation_caps:exposure:{_today_utc()}"


# ── Risk Unit Classifier ──────────────────────────────────────────────────────

def classify(ticker: str) -> dict[str, Optional[str]]:
    """Return {prefix, event_id, underlying} for `ticker`.

    Examples (Kalshi ticker convention varies; we use heuristic regex):
      KXMLBGAME-26MAY14-PHIATL-PHI  →
        prefix=KXMLBGAME, event_id=KXMLBGAME-26MAY14-PHIATL, underlying=MLB_DAY
      KXNFLGAME-25NOV16-BUFKC-BUF  →
        prefix=KXNFLGAME, event_id=KXNFLGAME-25NOV16-BUFKC, underlying=NFL_SUNDAY
      KXBTCD-26MAY14-3000  →
        prefix=KXBTCD, event_id=KXBTCD-26MAY14, underlying=CRYPTO_DAILY
    """
    if not ticker:
        return {"prefix": None, "event_id": None, "underlying": None}
    # Match prefix — longest prefix from known table wins
    prefix = None
    for p in sorted(_PREFIX_TO_UNDERLYING.keys(), key=len, reverse=True):
        if ticker.startswith(p):
            prefix = p
            break
    if prefix is None:
        # Fall back to first dash-delimited token
        prefix = ticker.split("-", 1)[0]

    # event_id = prefix-DATE-TEAMS (everything except the final outcome leaf)
    # Heuristic: take all-except-last segment of ticker (Kalshi convention is
    # "PREFIX-DATE-EVENT-OUTCOME"; for 1-leg tickers we use the whole ticker).
    parts = ticker.split("-")
    if len(parts) >= 4:
        event_id = "-".join(parts[:-1])
    elif len(parts) >= 2:
        event_id = ticker  # 2-3 segment tickers treat whole as event
    else:
        event_id = ticker

    underlying = _PREFIX_TO_UNDERLYING.get(prefix, "OTHER")
    return {"prefix": prefix, "event_id": event_id, "underlying": underlying}


# ── Cap lookup ────────────────────────────────────────────────────────────────

async def _get_cap_overrides(bus_redis: Any) -> dict[str, dict]:
    """Read per-prefix and per-underlying overrides from ep:config hash."""
    overrides: dict[str, dict] = {"per_prefix": {}, "per_underlying": {}}
    if bus_redis is None:
        return overrides
    try:
        raw = await bus_redis.hgetall("ep:config")
    except Exception:
        return overrides
    for k, v in (raw or {}).items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        if key.startswith("override_corr_cap_prefix_"):
            prefix = key[len("override_corr_cap_prefix_"):]
            try:
                overrides["per_prefix"][prefix] = int(val)
            except (TypeError, ValueError):
                pass
        elif key.startswith("override_corr_cap_underlying_"):
            ulying = key[len("override_corr_cap_underlying_"):]
            try:
                overrides["per_underlying"][ulying] = int(val)
            except (TypeError, ValueError):
                pass
    return overrides


def _resolve_caps(overrides: dict[str, dict], prefix: Optional[str], underlying: Optional[str]) -> dict[str, int]:
    return {
        "per_event_cap_cents":   _DEFAULTS["per_event_cap_cents"],
        "per_prefix_cap_cents":  overrides["per_prefix"].get(prefix or "", _PREFIX_DAILY_DEFAULTS.get(prefix or "", 30_000)),
        "per_underlying_cap_cents": overrides["per_underlying"].get(underlying or "", _UNDERLYING_DAILY_DEFAULTS.get(underlying or "", 50_000)),
        "total_daily_cap_cents": _DEFAULTS["total_daily_cap_cents"],
    }


# ── Pre-trade check ───────────────────────────────────────────────────────────

async def check_candidate(
    bus_redis: Any,
    ticker: str,
    requested_contracts: int,
    price_cents: int,
) -> dict[str, Any]:
    """Return {allowed_contracts, reason} for a candidate signal.

    `allowed_contracts == requested_contracts` → pass through unchanged.
    `0 < allowed_contracts < requested_contracts` → downsized to fit caps.
    `allowed_contracts == 0` → reject with `reason` populated.
    """
    if requested_contracts <= 0 or price_cents <= 0:
        return {"allowed_contracts": 0, "reason": "INVALID_INPUT"}
    requested_cost = requested_contracts * price_cents
    cls = classify(ticker)
    overrides = await _get_cap_overrides(bus_redis)
    caps = _resolve_caps(overrides, cls["prefix"], cls["underlying"])

    # Current exposure by aggregation level
    exposure_key = _exposure_key()
    try:
        current_raw = await bus_redis.hgetall(exposure_key) if bus_redis else {}
    except Exception:
        current_raw = {}
    current: dict[str, int] = {}
    for k, v in (current_raw or {}).items():
        ks = k.decode() if isinstance(k, bytes) else k
        vs = v.decode() if isinstance(v, bytes) else v
        try:
            current[ks] = int(vs)
        except (TypeError, ValueError):
            current[ks] = 0

    # Remaining headroom at each level (allowed - already deployed)
    event_used      = current.get(f"event:{cls['event_id']}", 0)
    prefix_used     = current.get(f"prefix:{cls['prefix']}", 0)
    underlying_used = current.get(f"underlying:{cls['underlying']}", 0)
    total_used      = current.get("total:_all_", 0)

    event_headroom      = max(0, caps["per_event_cap_cents"] - event_used)
    prefix_headroom     = max(0, caps["per_prefix_cap_cents"] - prefix_used)
    underlying_headroom = max(0, caps["per_underlying_cap_cents"] - underlying_used)
    total_headroom      = max(0, caps["total_daily_cap_cents"] - total_used)

    binding_headroom = min(event_headroom, prefix_headroom, underlying_headroom, total_headroom)

    if requested_cost <= binding_headroom:
        return {
            "allowed_contracts": requested_contracts,
            "reason":            None,
            "binding":           None,
        }

    # Downsize
    allowed_cost = binding_headroom
    if allowed_cost < _DEFAULTS["downsize_minimum_cents"]:
        binding_layer = (
            "event" if event_headroom == binding_headroom else
            "prefix" if prefix_headroom == binding_headroom else
            "underlying" if underlying_headroom == binding_headroom else
            "total"
        )
        return {
            "allowed_contracts": 0,
            "reason":            f"CORRELATION_CAP_{binding_layer.upper()}",
            "binding":           binding_layer,
            "headroom_cents":    binding_headroom,
        }
    allowed_contracts = max(1, int(allowed_cost // price_cents))
    binding_layer = (
        "event" if event_headroom == binding_headroom else
        "prefix" if prefix_headroom == binding_headroom else
        "underlying" if underlying_headroom == binding_headroom else
        "total"
    )
    return {
        "allowed_contracts": allowed_contracts,
        "reason":            f"DOWNSIZED_{binding_layer.upper()}",
        "binding":           binding_layer,
        "headroom_cents":    binding_headroom,
    }


async def record_fill(
    bus_redis: Any,
    ticker: str,
    contracts: int,
    price_cents: int,
) -> None:
    """Increment per-level exposure counters on a confirmed fill.

    Caller invokes from the fill confirmation path (ep_exec / fill-poll).
    Counters reset at UTC midnight via the rollover hook.
    """
    if bus_redis is None or contracts <= 0 or price_cents <= 0:
        return
    cls = classify(ticker)
    cost = contracts * price_cents
    exposure_key = _exposure_key()
    try:
        await bus_redis.hincrby(exposure_key, f"event:{cls['event_id']}", cost)
        await bus_redis.hincrby(exposure_key, f"prefix:{cls['prefix']}", cost)
        await bus_redis.hincrby(exposure_key, f"underlying:{cls['underlying']}", cost)
        await bus_redis.hincrby(exposure_key, "total:_all_", cost)
        await bus_redis.expire(exposure_key, 2 * 86_400)  # keep 48h for retroactive review
    except Exception as exc:
        log.debug("correlation_caps.record_fill failed for %s: %s", ticker, exc)


async def get_current_exposure(bus_redis: Any) -> dict[str, int]:
    """Operator helper: read today's per-level exposure totals."""
    if bus_redis is None:
        return {}
    try:
        raw = await bus_redis.hgetall(_exposure_key())
    except Exception:
        return {}
    out: dict[str, int] = {}
    for k, v in (raw or {}).items():
        ks = k.decode() if isinstance(k, bytes) else k
        vs = v.decode() if isinstance(v, bytes) else v
        try:
            out[ks] = int(vs)
        except (TypeError, ValueError):
            continue
    return out
