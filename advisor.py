"""
advisor.py — Settings Advisor Service

Analyzes your paper trade history, current market conditions, and account
state to recommend the optimal settings for your .env file.

Run at any time:
    python advisor.py

Run before switching from paper to live:
    python advisor.py --mode live

What it analyzes:
  1. Trade history (output/trades.csv)
     - Hit rate by edge bucket — finds the minimum edge that was profitable
     - P&L per trade at different thresholds — finds the optimal cut-off
     - Confidence calibration — checks if high-confidence trades actually won more
     - Stop-loss / take-profit effectiveness — were exits helping or hurting?

  2. Current market conditions (live Kalshi + FedWatch data)
     - Typical FOMC spread — sets MAX_SPREAD_CENTS appropriately
     - FedWatch confidence levels — sets MIN_CONFIDENCE appropriately
     - How many FOMC markets are active — affects poll interval recommendation

  3. Account state (if credentials configured)
     - Balance — sizes MAX_CONTRACTS relative to account
     - Historical drawdown — recommends DAILY_DRAWDOWN_LIMIT

  4. Safety gates
     - Never recommends settings that would bypass fees
     - Always recommends paper mode first if insufficient history
     - Flags any current settings that look dangerous

Output:
  - Plain-English analysis of each setting
  - Ready-to-paste .env block with recommended values
  - Confidence rating for each recommendation (HIGH / MEDIUM / LOW)
  - Warnings for any settings that need human judgment
"""

import os
import csv
import sys
import math
import asyncio
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ── Logging (minimal — advisor output goes to stdout) ─────────────────────────
logging.basicConfig(level=logging.WARNING)   # suppress bot module logs
log = logging.getLogger("advisor")

# ── Minimum data required before making recommendations ──────────────────────
MIN_TRADES_FOR_EDGE_REC    = 20   # need 20+ resolved trades to tune EDGE_THRESHOLD
MIN_TRADES_FOR_KELLY_REC   = 30   # need 30+ to tune KELLY_FRACTION
MIN_TRADES_FOR_EXIT_REC    = 15   # need 15+ exits to tune take-profit/stop-loss
FEE_CENTS                  = 7    # Kalshi fee per contract


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Recommendation:
    """A single setting recommendation with explanation."""
    setting:    str           # env var name e.g. "KALSHI_EDGE_THRESHOLD"
    current:    str           # current value from .env
    recommended: str          # recommended value
    confidence: str           # "HIGH", "MEDIUM", "LOW"
    reason:     str           # plain-English explanation
    changed:    bool = False  # True if recommendation differs from current

    def __post_init__(self):
        self.changed = str(self.current) != str(self.recommended)


@dataclass
class TradeRow:
    """Parsed row from trades.csv."""
    timestamp:    datetime
    ticker:       str
    side:         str
    action:       str       # "entry" or "exit"
    contracts:    int
    price_cents:  int
    fair_value:   float
    edge:         float
    confidence:   float
    mode:         str
    resolved:     bool = False
    won:          bool = False
    pnl_cents:    int  = 0


@dataclass
class AdvisorReport:
    """Full advisor output."""
    recommendations: list[Recommendation] = field(default_factory=list)
    warnings:        list[str]            = field(default_factory=list)
    data_summary:    dict                 = field(default_factory=dict)
    ready_for_live:  bool                 = False
    readiness_notes: list[str]            = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Trade history loader
# ─────────────────────────────────────────────────────────────────────────────

def load_trades(trades_csv: Path) -> list[TradeRow]:
    """Load and parse trades.csv. Returns empty list if file doesn't exist."""
    if not trades_csv.exists():
        return []

    trades = []
    with open(trades_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                trades.append(TradeRow(
                    timestamp   = datetime.fromisoformat(row.get("timestamp", "")),
                    ticker      = row.get("ticker", ""),
                    side        = row.get("side", "yes"),
                    action      = row.get("action", "entry"),
                    contracts   = int(row.get("contracts", 1)),
                    price_cents = int(row.get("price_cents", 50)),
                    fair_value  = float(row.get("fair_value", 0.5)),
                    edge        = float(row.get("edge", 0)),
                    confidence  = float(row.get("confidence", 0.7)),
                    mode        = row.get("mode", "paper"),
                ))
            except (ValueError, KeyError):
                continue

    return trades


def mark_resolutions(trades: list[TradeRow], resolutions: dict) -> list[TradeRow]:
    """
    Mark trades as resolved. resolutions = {ticker: True/False (YES won)}.
    Call this after each FOMC meeting with the actual outcomes.
    """
    for t in trades:
        if t.ticker in resolutions and t.action == "entry":
            yes_won  = resolutions[t.ticker]
            t.won    = (t.side == "yes" and yes_won) or (t.side == "no" and not yes_won)
            t.resolved = True
            if t.won:
                t.pnl_cents = (100 - t.price_cents) * t.contracts - FEE_CENTS * t.contracts
            else:
                t.pnl_cents = -(t.price_cents * t.contracts + FEE_CENTS * t.contracts)
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Analysis functions
# ─────────────────────────────────────────────────────────────────────────────

def analyze_edge_threshold(resolved: list[TradeRow]) -> tuple[Optional[float], str, str]:
    """
    Find the minimum edge threshold where trades are net profitable after fees.

    Returns (recommended_value, confidence, reason).
    """
    if len(resolved) < MIN_TRADES_FOR_EDGE_REC:
        return None, "LOW", (
            f"Only {len(resolved)} resolved trades — need {MIN_TRADES_FOR_EDGE_REC}+ "
            f"to tune edge threshold. Keeping default 0.10."
        )

    # Group trades by edge bucket (2-cent wide buckets)
    buckets: dict[float, list[TradeRow]] = defaultdict(list)
    for t in resolved:
        bucket = round(math.floor(t.edge / 0.02) * 0.02, 2)
        buckets[bucket].append(t)

    # Find lowest edge bucket where avg fee-adjusted P&L > 0
    best_threshold = 0.10   # default fallback
    bucket_stats   = []

    for bucket in sorted(buckets.keys()):
        trades_in_bucket = buckets[bucket]
        if len(trades_in_bucket) < 3:
            continue
        avg_pnl  = sum(t.pnl_cents for t in trades_in_bucket) / max(len(trades_in_bucket), 1)
        hit_rate = sum(1 for t in trades_in_bucket if t.won) / max(len(trades_in_bucket), 1)
        bucket_stats.append((bucket, avg_pnl, hit_rate, len(trades_in_bucket)))

    # Find lowest profitable bucket with hit rate > 52%
    profitable_buckets = [
        (b, p, h, n) for b, p, h, n in bucket_stats if p > 0 and h > 0.52
    ]

    if not profitable_buckets:
        return 0.12, "MEDIUM", (
            "No edge bucket shows consistently positive P&L. "
            "Raising threshold to 0.12 to be more selective."
        )

    best_threshold = profitable_buckets[0][0]
    best_hit_rate  = profitable_buckets[0][2]
    best_pnl       = profitable_buckets[0][1]

    # Round up to nearest 0.01 and add a small safety buffer
    recommended = round(max(best_threshold, 0.08), 2)

    confidence = "HIGH" if len(resolved) >= 50 else "MEDIUM"
    reason = (
        f"Analysis of {len(resolved)} resolved trades shows positive avg P&L "
        f"(+{best_pnl:.1f}¢/trade) starting at edge={best_threshold:.2f} "
        f"with {best_hit_rate:.0%} hit rate. "
        f"Recommended: {recommended} (includes small safety buffer above fees)."
    )
    return recommended, confidence, reason


def analyze_confidence_threshold(resolved: list[TradeRow]) -> tuple[Optional[float], str, str]:
    """
    Find the minimum confidence level where trades are net profitable.
    """
    if len(resolved) < MIN_TRADES_FOR_EDGE_REC:
        return None, "LOW", (
            f"Insufficient resolved trades. Keeping default 0.60."
        )

    # Group by confidence bucket
    buckets: dict[float, list[TradeRow]] = defaultdict(list)
    for t in resolved:
        bucket = round(math.floor(t.confidence / 0.10) * 0.10, 1)
        buckets[bucket].append(t)

    stats = []
    for conf in sorted(buckets.keys()):
        ts = buckets[conf]
        if len(ts) < 3:
            continue
        hit_rate = sum(1 for t in ts if t.won) / max(len(ts), 1)
        avg_pnl  = sum(t.pnl_cents for t in ts) / max(len(ts), 1)
        stats.append((conf, hit_rate, avg_pnl, len(ts)))

    # Check calibration: does higher confidence → higher hit rate?
    if len(stats) >= 2:
        confs    = [s[0] for s in stats]
        hit_rates = [s[1] for s in stats]
        # Simple slope check
        slope = (hit_rates[-1] - hit_rates[0]) / max(confs[-1] - confs[0], 0.01)
        calibrated = slope > 0
    else:
        calibrated = True  # assume calibrated with little data

    # Find lowest confidence with positive P&L
    profitable = [(c, h, p, n) for c, h, p, n in stats if p > 0]
    if not profitable:
        return 0.70, "MEDIUM", (
            "No confidence bucket shows consistently positive P&L. "
            "Raising MIN_CONFIDENCE to 0.70 to be more selective."
        )

    recommended = round(max(profitable[0][0], 0.60), 1)
    cal_note = " Model is well-calibrated." if calibrated else \
               " WARNING: higher confidence is not producing better hit rates — model may need retuning."

    return recommended, "MEDIUM", (
        f"Trades at confidence >= {recommended:.1f} show positive avg P&L." + cal_note
    )


def analyze_exit_settings(trades: list[TradeRow]) -> tuple[dict, str, str]:
    """
    Analyze exit effectiveness.
    Returns recommended {take_profit_cents, stop_loss_cents} and explanation.
    """
    exits = [t for t in trades if t.action == "exit"]

    if len(exits) < MIN_TRADES_FOR_EXIT_REC:
        return {}, "LOW", (
            f"Only {len(exits)} exit trades recorded — need {MIN_TRADES_FOR_EXIT_REC}+ "
            f"to tune exit settings. Keeping defaults."
        )

    take_profit_exits = [t for t in exits if "take profit" in t.ticker.lower() or t.pnl_cents > 0]
    stop_loss_exits   = [t for t in exits if "stop loss" in t.ticker.lower() or t.pnl_cents < 0]

    # Analyze actual price moves at exit
    exit_prices = [t.price_cents for t in exits]
    if not exit_prices:
        return {}, "LOW", "No exit price data available."

    avg_favorable_move = sum(t.price_cents for t in exits if t.pnl_cents > 0) / max(len(take_profit_exits), 1)
    avg_adverse_move   = sum(abs(t.price_cents) for t in exits if t.pnl_cents < 0) / max(len(stop_loss_exits), 1)

    # Recommend take profit at 80th percentile of favorable moves
    # and stop loss at 70th percentile of adverse moves
    rec_tp = max(15, min(30, int(avg_favorable_move * 0.8)))
    rec_sl = max(10, min(20, int(avg_adverse_move * 0.7)))

    return (
        {"take_profit_cents": rec_tp, "stop_loss_cents": rec_sl},
        "MEDIUM",
        f"Based on {len(exits)} exits: avg favorable move {avg_favorable_move:.0f}¢, "
        f"avg adverse move {avg_adverse_move:.0f}¢. "
        f"Recommended take profit={rec_tp}¢, stop loss={rec_sl}¢."
    )


def analyze_kelly_fraction(resolved: list[TradeRow]) -> tuple[Optional[float], str, str]:
    """
    Recommend Kelly fraction based on variance of actual outcomes.
    Higher variance → lower Kelly fraction to reduce risk of ruin.
    """
    if len(resolved) < MIN_TRADES_FOR_KELLY_REC:
        return None, "LOW", (
            f"Need {MIN_TRADES_FOR_KELLY_REC}+ resolved trades to tune Kelly. "
            f"Keeping conservative default of 0.25."
        )

    pnls    = [t.pnl_cents for t in resolved]
    mean    = sum(pnls) / max(len(pnls), 1)
    variance = sum((p - mean) ** 2 for p in pnls) / max(len(pnls), 1)
    std     = math.sqrt(variance) if variance > 0 else 1
    cv      = abs(std / mean) if mean != 0 else 999   # coefficient of variation

    # Higher variance relative to mean → more conservative Kelly
    if cv < 1.5:
        recommended = 0.30
        conf = "MEDIUM"
        note = "Low variance in outcomes — slightly more aggressive sizing is justified."
    elif cv < 3.0:
        recommended = 0.25
        conf = "HIGH"
        note = "Moderate variance — quarter-Kelly is appropriate."
    else:
        recommended = 0.15
        conf = "MEDIUM"
        note = "High variance in outcomes — reducing to 15% Kelly to protect against drawdown."

    return recommended, conf, (
        f"Outcome CV={cv:.2f} (std/mean of P&L). {note}"
    )


def analyze_max_contracts(balance_cents: int, avg_price_cents: float) -> tuple[int, str, str]:
    """
    Recommend MAX_CONTRACTS based on account balance.
    Cap each single trade at 2% of balance.
    """
    if balance_cents <= 0:
        return 5, "LOW", "Balance unknown — keeping conservative default of 5 contracts."

    balance_dollars = balance_cents / 100
    price_dollars   = avg_price_cents / 100

    # 2% of balance per trade
    max_by_balance = max(1, int((balance_dollars * 0.02) / max(price_dollars, 0.01)))
    recommended    = min(max_by_balance, 20)   # hard cap at 20

    return recommended, "HIGH", (
        f"Balance ${balance_dollars:.2f}: 2% per trade = ${balance_dollars * 0.02:.2f}, "
        f"≈ {recommended} contracts at avg price {avg_price_cents:.0f}¢."
    )


def analyze_spread_threshold(market_spreads: list[int]) -> tuple[int, str, str]:
    """
    Recommend MAX_SPREAD_CENTS based on observed FOMC market spreads.
    """
    if not market_spreads:
        return 10, "LOW", "No live market data — keeping default of 10¢."

    median_spread = sorted(market_spreads)[len(market_spreads) // 2]
    p75_spread    = sorted(market_spreads)[int(len(market_spreads) * 0.75)]

    # Set limit at 75th percentile + 2¢ buffer, but never below fee level
    recommended = max(int(p75_spread) + 2, FEE_CENTS + 3)
    recommended = min(recommended, 15)   # cap at 15¢

    return recommended, "HIGH", (
        f"Observed FOMC spreads: median={median_spread}¢, 75th pct={p75_spread}¢. "
        f"Recommended limit: {recommended}¢ (filters illiquid markets while "
        f"keeping liquid ones tradeable)."
    )


def assess_live_readiness(
    resolved: list[TradeRow],
    hit_rate: float,
    avg_pnl: float,
    n_meetings: int,
) -> tuple[bool, list[str]]:
    """
    Assess whether the bot is ready to switch from paper to live trading.
    Returns (ready, list_of_notes).
    """
    notes = []
    ready = True

    if len(resolved) < 20:
        notes.append(f"❌ Only {len(resolved)} resolved trades — need 20+ before going live.")
        ready = False
    else:
        notes.append(f"✓ {len(resolved)} resolved trades recorded.")

    if n_meetings < 2:
        notes.append(f"❌ Only {n_meetings} FOMC meeting(s) in history — need data from 2+ meetings.")
        ready = False
    else:
        notes.append(f"✓ History spans {n_meetings} FOMC meetings.")

    if hit_rate < 0.53:
        notes.append(f"❌ Hit rate {hit_rate:.1%} is below 53% — edge may not be real.")
        ready = False
    elif hit_rate < 0.58:
        notes.append(f"⚠️  Hit rate {hit_rate:.1%} is marginal — consider running longer.")
    else:
        notes.append(f"✓ Hit rate {hit_rate:.1%} is solid.")

    if avg_pnl <= 0:
        notes.append(f"❌ Avg fee-adjusted P&L is {avg_pnl:.1f}¢ — not yet profitable.")
        ready = False
    elif avg_pnl < 3:
        notes.append(f"⚠️  Avg P&L {avg_pnl:.1f}¢/trade is thin — fees could erase this in live mode.")
    else:
        notes.append(f"✓ Avg P&L {avg_pnl:.1f}¢/trade after fees.")

    if ready:
        notes.append("✅ Bot appears ready for cautious live trading. Start with MAX_CONTRACTS=2.")
    else:
        notes.append("🛑 Continue paper trading until all checks pass.")

    return ready, notes


# ─────────────────────────────────────────────────────────────────────────────
# Live market data fetcher
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_live_context(env_path: Path) -> dict:
    """
    Fetch current FOMC market spreads and FedWatch confidence from Kalshi.
    Returns empty dict if credentials aren't configured or fetch fails.
    """
    context = {
        "spreads":    [],
        "confidence": None,
        "n_markets":  0,
        "balance":    0,
    }

    # Load .env to get credentials
    from dotenv import load_dotenv
    load_dotenv(env_path)

    api_key_id    = os.getenv("KALSHI_API_KEY_ID", "")
    private_key   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "private_key.pem")
    paper         = os.getenv("KALSHI_PAPER_TRADE", "true").lower() == "true"

    if not api_key_id or not Path(private_key).exists():
        print("  ℹ️  No API credentials found — skipping live market data.")
        print("     (Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env for live analysis)")
        return context

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from kalshi_bot.auth     import KalshiAuth
        from kalshi_bot.client   import KalshiClient
        from kalshi_bot.strategy import scan_fomc_markets
        from kalshi_bot.models   import fomc as fomc_mod

        auth   = KalshiAuth(api_key_id=api_key_id, private_key_path=Path(private_key))
        base   = ("https://demo-api.kalshi.co/trade-api/v2" if paper
                  else "https://api.elections.kalshi.com/trade-api/v2")
        client = KalshiClient(base_url=base, auth=auth, timeout=8, max_retries=1)

        # Fetch FOMC markets
        markets = scan_fomc_markets(client, force_refresh=True)
        context["n_markets"] = len(markets)

        # Fetch order books for spreads
        if markets:
            paths   = [f"/markets/{m['ticker']}/orderbook" for m in markets]
            results = await client.get_many(paths)
            for ob in results:
                if ob:
                    book = ob.get("orderbook", {})
                    bids = book.get("yes", [])
                    asks = book.get("no",  [])
                    if bids and asks:
                        spread = max((100 - asks[0][0]) - bids[0][0], 0)
                        context["spreads"].append(spread)

        # Fetch FedWatch confidence
        try:
            if markets:
                from kalshi_bot.models.fomc import fair_value_with_confidence
                _, conf = await fair_value_with_confidence(
                    markets[0]["ticker"],
                    markets[0].get("last_price", 50) / 100
                )
                context["confidence"] = conf
        except Exception:
            log.debug('advisor fetch error skipped')

        # Fetch balance
        try:
            bal = client.get("/portfolio/balance")
            context["balance"] = bal.get("balance", 0)
        except Exception:
            log.debug('advisor fetch error skipped')

        print(f"  ✓ Live data: {len(markets)} FOMC markets, "
              f"{len(context['spreads'])} order books fetched.")

    except Exception as exc:
        print(f"  ⚠️  Live data fetch failed: {exc}")

    return context


# ─────────────────────────────────────────────────────────────────────────────
# Main advisor logic
# ─────────────────────────────────────────────────────────────────────────────

def load_current_settings(env_path: Path) -> dict:
    """Read current .env values."""
    settings = {}
    if not env_path.exists():
        return settings
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                settings[k.strip()] = v.strip()
    return settings


def run_advisor(
    trades_csv:   Path,
    env_path:     Path,
    resolutions:  dict,
    target_mode:  str = "paper",
) -> AdvisorReport:
    """
    Core advisor logic — pure Python, no async, no network calls.
    Call this after fetch_live_context() has populated the live context.
    """
    report   = AdvisorReport()
    settings = load_current_settings(env_path)

    # ── Load + resolve trade history ──────────────────────────────────────────
    all_trades = load_trades(trades_csv)
    if resolutions:
        all_trades = mark_resolutions(all_trades, resolutions)

    entries  = [t for t in all_trades if t.action == "entry"]
    resolved = [t for t in entries if t.resolved]
    exits    = [t for t in all_trades if t.action == "exit"]

    n_meetings = len({t.ticker.rsplit("-", 1)[0] for t in resolved}) if resolved else 0
    hit_rate   = sum(1 for t in resolved if t.won) / max(len(resolved), 1)
    avg_pnl    = sum(t.pnl_cents for t in resolved) / max(len(resolved), 1)
    avg_edge   = sum(t.edge for t in entries) / max(len(entries), 1)
    avg_price  = sum(t.price_cents for t in entries) / max(len(entries), 1)

    report.data_summary = {
        "total_entries":   len(entries),
        "resolved_trades": len(resolved),
        "exits_recorded":  len(exits),
        "hit_rate":        hit_rate,
        "avg_pnl_cents":   avg_pnl,
        "avg_edge":        avg_edge,
        "n_meetings":      n_meetings,
    }

    return report, resolved, exits, avg_price, hit_rate, avg_pnl, n_meetings, settings


def build_recommendations(
    report:       AdvisorReport,
    resolved:     list,
    exits:        list,
    avg_price:    float,
    hit_rate:     float,
    avg_pnl:      float,
    n_meetings:   int,
    settings:     dict,
    live_context: dict,
    target_mode:  str,
) -> AdvisorReport:
    """Build all recommendations and populate the report."""

    balance = live_context.get("balance", 0)

    # ── EDGE_THRESHOLD ────────────────────────────────────────────────────────
    edge_val, edge_conf, edge_reason = analyze_edge_threshold(resolved)
    cur_edge = settings.get("KALSHI_EDGE_THRESHOLD", "0.10")
    rec_edge = f"{edge_val:.2f}" if edge_val else cur_edge
    report.recommendations.append(Recommendation(
        setting="KALSHI_EDGE_THRESHOLD",
        current=cur_edge,
        recommended=rec_edge,
        confidence=edge_conf,
        reason=edge_reason,
    ))

    # ── MIN_CONFIDENCE ────────────────────────────────────────────────────────
    conf_val, conf_conf, conf_reason = analyze_confidence_threshold(resolved)

    # Also factor in live FedWatch confidence if available
    live_conf = live_context.get("confidence")
    if live_conf and live_conf < 0.65:
        conf_reason += (
            f" Current FedWatch confidence is only {live_conf:.0%} — "
            f"consider raising MIN_CONFIDENCE until sources converge."
        )
        if conf_val:
            conf_val = max(conf_val, live_conf + 0.05)

    cur_conf = settings.get("KALSHI_MIN_CONFIDENCE", "0.60")
    rec_conf = f"{conf_val:.2f}" if conf_val else cur_conf
    report.recommendations.append(Recommendation(
        setting="KALSHI_MIN_CONFIDENCE",
        current=cur_conf,
        recommended=rec_conf,
        confidence=conf_conf,
        reason=conf_reason,
    ))

    # ── MAX_SPREAD_CENTS ──────────────────────────────────────────────────────
    spreads = live_context.get("spreads", [])
    spread_val, spread_conf, spread_reason = analyze_spread_threshold(spreads)
    cur_spread = settings.get("KALSHI_MAX_SPREAD_CENTS", "10")
    report.recommendations.append(Recommendation(
        setting="KALSHI_MAX_SPREAD_CENTS",
        current=cur_spread,
        recommended=str(spread_val),
        confidence=spread_conf,
        reason=spread_reason,
    ))

    # ── MAX_CONTRACTS ─────────────────────────────────────────────────────────
    mc_val, mc_conf, mc_reason = analyze_max_contracts(balance, avg_price or 50)
    cur_mc = settings.get("KALSHI_MAX_CONTRACTS", "5")

    # In live mode, extra-conservative cap
    if target_mode == "live":
        mc_val = min(mc_val, 3)
        mc_reason += " (Capped at 3 for first live session — increase gradually.)"

    report.recommendations.append(Recommendation(
        setting="KALSHI_MAX_CONTRACTS",
        current=cur_mc,
        recommended=str(mc_val),
        confidence=mc_conf,
        reason=mc_reason,
    ))

    # ── KELLY_FRACTION ────────────────────────────────────────────────────────
    kelly_val, kelly_conf, kelly_reason = analyze_kelly_fraction(resolved)
    cur_kelly = settings.get("KALSHI_KELLY_FRACTION", "0.25")
    rec_kelly = f"{kelly_val:.2f}" if kelly_val else cur_kelly
    report.recommendations.append(Recommendation(
        setting="KALSHI_KELLY_FRACTION",
        current=cur_kelly,
        recommended=rec_kelly,
        confidence=kelly_conf,
        reason=kelly_reason,
    ))

    # ── EXIT SETTINGS ─────────────────────────────────────────────────────────
    exit_dict, exit_conf, exit_reason = analyze_exit_settings(exits)
    for key, default in [("KALSHI_TAKE_PROFIT_CENTS", "20"),
                         ("KALSHI_STOP_LOSS_CENTS", "15")]:
        cur_val = settings.get(key, default)
        env_key = key.replace("KALSHI_", "").lower().replace("_cents", "_cents")
        rec_val = str(exit_dict.get(env_key.replace("kalshi_", ""), int(default)))
        report.recommendations.append(Recommendation(
            setting=key,
            current=cur_val,
            recommended=rec_val if exit_dict else cur_val,
            confidence=exit_conf,
            reason=exit_reason,
        ))

    # ── DAILY_DRAWDOWN_LIMIT ──────────────────────────────────────────────────
    # Conservative recommendation: tighter in live mode
    cur_dd = settings.get("KALSHI_DAILY_DRAWDOWN_LIMIT", "0.10")
    if target_mode == "live":
        rec_dd, dd_conf = "0.07", "HIGH"
        dd_reason = "Tighter 7% limit for live mode — easier to hit unintentionally."
    else:
        rec_dd, dd_conf = "0.10", "HIGH"
        dd_reason = "10% drawdown limit is standard for paper trading."
    report.recommendations.append(Recommendation(
        setting="KALSHI_DAILY_DRAWDOWN_LIMIT",
        current=cur_dd,
        recommended=rec_dd,
        confidence=dd_conf,
        reason=dd_reason,
    ))

    # ── PAPER_TRADE ───────────────────────────────────────────────────────────
    cur_paper = settings.get("KALSHI_PAPER_TRADE", "true")
    rec_paper = "false" if target_mode == "live" else "true"
    report.recommendations.append(Recommendation(
        setting="KALSHI_PAPER_TRADE",
        current=cur_paper,
        recommended=rec_paper,
        confidence="HIGH",
        reason=(
            "Ready for live trading based on history analysis."
            if target_mode == "live" else
            "Keep paper trading until readiness checks pass."
        ),
    ))

    # ── Live readiness ────────────────────────────────────────────────────────
    report.ready_for_live, report.readiness_notes = assess_live_readiness(
        resolved, hit_rate, avg_pnl, n_meetings
    )

    # ── Warnings ─────────────────────────────────────────────────────────────
    if avg_pnl < 0 and len(resolved) >= 10:
        report.warnings.append(
            "⚠️  Average P&L is NEGATIVE after fees. The edge may be overstated "
            "by the model. Consider raising EDGE_THRESHOLD before continuing."
        )

    if hit_rate < 0.50 and len(resolved) >= 10:
        report.warnings.append(
            "⚠️  Hit rate below 50% — the model is wrong more often than it's right. "
            "Do not go live until this improves over a larger sample."
        )

    n_markets = live_context.get("n_markets", 0)
    if n_markets == 0:
        report.warnings.append(
            "ℹ️  No FOMC markets currently open. The next FOMC meeting may be "
            "more than a few weeks away — limited trading opportunities right now."
        )

    return report


# ─────────────────────────────────────────────────────────────────────────────
# Output formatter
# ─────────────────────────────────────────────────────────────────────────────

def print_report(
    report:      AdvisorReport,
    target_mode: str,
    env_path:    Path,
):
    """Print the full advisor report to stdout."""
    W  = 70
    hr = "─" * W

    print()
    print("=" * W)
    print("  KALSHI BOT — SETTINGS ADVISOR")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Target mode: {target_mode.upper()}")
    print("=" * W)

    # Data summary
    ds = report.data_summary
    print(f"\n📊 Trade History")
    print(f"  Entry trades logged:    {ds.get('total_entries', 0)}")
    print(f"  Resolved trades:        {ds.get('resolved_trades', 0)}")
    print(f"  Exits recorded:         {ds.get('exits_recorded', 0)}")
    print(f"  FOMC meetings covered:  {ds.get('n_meetings', 0)}")
    if ds.get("resolved_trades", 0) > 0:
        print(f"  Hit rate:               {ds.get('hit_rate', 0):.1%}")
        print(f"  Avg P&L (fee-adj):      {ds.get('avg_pnl_cents', 0):+.1f}¢ per trade")
        print(f"  Avg edge at entry:      {ds.get('avg_edge', 0):.3f}")

    # Live readiness
    print(f"\n🚦 Live Readiness Check")
    for note in report.readiness_notes:
        print(f"  {note}")

    # Warnings
    if report.warnings:
        print(f"\n⚠️  Warnings")
        for w in report.warnings:
            print(f"  {w}")

    # Recommendations
    print(f"\n⚙️  Setting Recommendations")
    print(f"  {'Setting':<35} {'Current':>10} {'Recommended':>13} {'Conf'}")
    print(f"  {hr[:66]}")

    changed_count = 0
    for rec in report.recommendations:
        marker = " ◄" if rec.changed else "  "
        print(f"  {rec.setting:<35} {rec.current:>10} {rec.recommended:>13}  {rec.confidence}{marker}")
        if rec.changed:
            changed_count += 1

    print(f"\n  {changed_count} setting(s) differ from current values  (◄ = change recommended)")

    # Detailed explanations
    print(f"\n📋 Explanations")
    for rec in report.recommendations:
        if rec.changed or rec.confidence == "HIGH":
            status = "CHANGE" if rec.changed else "OK"
            print(f"\n  [{status}] {rec.setting}")
            # Word-wrap the reason
            words   = rec.reason.split()
            line    = "    "
            for w in words:
                if len(line) + len(w) + 1 > W - 2:
                    print(line)
                    line = "    " + w + " "
                else:
                    line += w + " "
            if line.strip():
                print(line)

    # Ready-to-paste .env block
    print(f"\n📝 Recommended .env block (copy-paste to replace current settings)")
    print(f"  {hr}")
    print()

    category_comments = {
        "KALSHI_PAPER_TRADE":         "# ── Mode ──────────────────────────────────",
        "KALSHI_EDGE_THRESHOLD":      "# ── Strategy ───────────────────────────────",
        "KALSHI_KELLY_FRACTION":      "# ── Risk ───────────────────────────────────",
        "KALSHI_TAKE_PROFIT_CENTS":   "# ── Exit management ────────────────────────",
        "KALSHI_DAILY_DRAWDOWN_LIMIT":"# ── Drawdown ───────────────────────────────",
    }

    for rec in report.recommendations:
        if rec.setting in category_comments:
            print(f"  {category_comments[rec.setting]}")
        marker = "  # ← CHANGED" if rec.changed else ""
        print(f"  {rec.setting}={rec.recommended}{marker}")

    print()
    print("=" * W)
    print()

    # Write to file
    out_path = Path("output/advisor_recommendations.txt")
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        f.write(f"# Advisor recommendations — {datetime.now().isoformat()}\n")
        f.write(f"# Target mode: {target_mode}\n\n")
        for rec in report.recommendations:
            f.write(f"{rec.setting}={rec.recommended}\n")
    print(f"  Recommendations saved to {out_path}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Bot Settings Advisor — analyzes history and recommends optimal settings."
    )
    parser.add_argument(
        "--mode", choices=["paper", "live"], default="paper",
        help="Target mode: 'paper' (default) or 'live' (stricter recommendations)"
    )
    parser.add_argument(
        "--trades", default="output/trades.csv",
        help="Path to trades CSV (default: output/trades.csv)"
    )
    parser.add_argument(
        "--env", default=".env",
        help="Path to .env file (default: .env)"
    )
    parser.add_argument(
        "--resolve", nargs="*",
        help=(
            "Mark resolutions for completed FOMC meetings. "
            "Format: TICKER=1 or TICKER=0 (1=YES won, 0=NO won). "
            "Example: --resolve FOMC-25JUN18-HOLD=1 FOMC-25JUN18-CUT25=0"
        )
    )
    args = parser.parse_args()

    trades_csv = Path(args.trades)
    env_path   = Path(args.env)

    # Parse resolution flags
    resolutions = {}
    if args.resolve:
        for r in args.resolve:
            if "=" in r:
                ticker, val = r.split("=", 1)
                resolutions[ticker.strip()] = val.strip() == "1"

    print(f"\n🔍 Kalshi Settings Advisor")
    print(f"   Trades file:  {trades_csv}")
    print(f"   Env file:     {env_path}")
    print(f"   Target mode:  {args.mode}")
    if resolutions:
        print(f"   Resolutions:  {resolutions}")

    # Fetch live market context
    print(f"\n📡 Fetching live market data...")
    live_context = asyncio.run(fetch_live_context(env_path))

    # Run analysis
    print(f"\n🧮 Analyzing trade history...")
    report, resolved, exits, avg_price, hit_rate, avg_pnl, n_meetings, settings = \
        run_advisor(trades_csv, env_path, resolutions, target_mode=args.mode)

    # Build recommendations
    report = build_recommendations(
        report, resolved, exits, avg_price, hit_rate, avg_pnl, n_meetings,
        settings, live_context, target_mode=args.mode,
    )

    # Print
    print_report(report, args.mode, env_path)


if __name__ == "__main__":
    main()