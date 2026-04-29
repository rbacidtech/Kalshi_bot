"""
backtest_terminal.py — Terminal-outcome backtest for EdgePulse trades.

Replays every entry in output/trades.csv against the actual Kalshi market
state via the public /trade-api/v2/markets/{ticker} endpoint:

  - For RESOLVED markets   -> P&L per contract at the YES/NO outcome.
  - For UNRESOLVED markets -> mark-to-market P&L at the current mid price.

Compares against the bot's realized "book" P&L (entry + bot-driven exit)
to expose where the TP/SL/time-before-close exit policy leaves money on
the table — and where it correctly cuts a loser before resolution.

The Becker-paper analysis approach was rejected because the public Becker
parquet snapshot (last-modified 2026-02-05) does not cover any of this
bot's tickers, all of which were entered in April 2026. This backtest
uses Kalshi's authoritative outcome instead — the same ground truth Becker
collected, but for our actual window.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

EDGEPULSE   = Path("/root/EdgePulse")
TRADES_CSV  = EDGEPULSE / "output" / "trades.csv"
CACHE_FILE  = EDGEPULSE / "output" / "backtest_market_cache.json"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ── Bucket helpers ────────────────────────────────────────────────────────────

def category_of(ticker: str) -> str:
    m = re.match(r"KX([A-Z]+)", ticker)
    if not m:
        return "other"
    p = m.group(1)
    if p == "FED":
        return "fomc"
    if p == "GDP":
        return "gdp"
    if p.startswith("HIGH") or p.startswith("LOW") or p.startswith("TEMP"):
        return "weather"
    return p.lower()


def cost_bucket(eff_yes_cents: int) -> str:
    if eff_yes_cents < 20:
        return "longshot_<20c"
    if eff_yes_cents < 40:
        return "20-40c"
    if eff_yes_cents < 60:
        return "40-60c"
    if eff_yes_cents < 80:
        return "60-80c"
    return ">=80c"


# ── Kalshi market fetcher with disk cache ─────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def fetch_market(ticker: str, cache: dict, retries: int = 3) -> dict | None:
    """Return the Kalshi market record (or None on 404)."""
    if ticker in cache:
        return cache[ticker]

    url = f"{KALSHI_BASE}/markets/{ticker}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "edgepulse-backtest/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            market = data.get("market") or {}
            cache[ticker] = market
            return market
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                cache[ticker] = {}  # negative cache
                return {}
            time.sleep(0.5 * (attempt + 1))
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    print(f"  WARN: failed to fetch {ticker}", file=sys.stderr)
    return None


# ── Per-entry P&L computation ─────────────────────────────────────────────────

@dataclass
class EntryPnL:
    ticker: str
    side: str               # "yes" | "no"
    contracts: int
    entry_yes_cents: int    # entry YES price * 100 (executor.py records this for both sides)
    fair_value: float
    confidence: float
    edge: float
    model_source: str
    category: str
    bucket: str
    status: str             # "resolved_yes" | "resolved_no" | "open" | "unknown"
    terminal_yes_cents: int | None  # 100/0 if resolved, current mid else
    pnl_per_contract: int | None    # cents per contract; None if unknown
    is_terminal: bool


def per_contract_pnl(side: str, entry_yes: int, terminal_yes: int) -> int:
    """Cents P&L per contract.

    YES position: paid entry_yes, contract worth terminal_yes -> terminal_yes - entry_yes.
    NO  position: paid 100-entry_yes, NO contract worth 100-terminal_yes ->
                  (100 - terminal_yes) - (100 - entry_yes) = entry_yes - terminal_yes.
    Reduces to: side=="yes" -> terminal-entry; side=="no" -> entry-terminal. (Gross of fees.)
    """
    if side == "yes":
        return terminal_yes - entry_yes
    return entry_yes - terminal_yes


def classify_market(market: dict) -> tuple[str, int | None, bool]:
    """Return (status, terminal_yes_cents, is_terminal).

    status        — "resolved_yes" | "resolved_no" | "open" | "unknown"
    terminal_yes  — 100/0 if resolved; current mid (cents) if open; None if unknown
    is_terminal   — True only if Kalshi reports a result.
    """
    if not market:
        return "unknown", None, False

    result = (market.get("result") or "").strip().lower()
    if result == "yes":
        return "resolved_yes", 100, True
    if result == "no":
        return "resolved_no", 0, True

    # Open / settled-without-result fallback to current mid.
    bid = float(market.get("yes_bid_dollars") or 0)
    ask = float(market.get("yes_ask_dollars") or 0)
    last = float(market.get("last_price_dollars") or 0)
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
    elif last > 0:
        mid = last
    else:
        return "unknown", None, False
    return "open", int(round(mid * 100)), False


# ── Replay ────────────────────────────────────────────────────────────────────

def replay() -> list[EntryPnL]:
    cache = load_cache()
    pnls: list[EntryPnL] = []

    # Read every entry first so we can pre-fetch unique tickers in one pass.
    rows: list[dict] = []
    with TRADES_CSV.open() as f:
        for row in csv.DictReader(f):
            if row.get("action") == "entry":
                rows.append(row)
    tickers = sorted({r["ticker"] for r in rows})

    print(f"Entries in trades.csv: {len(rows)}  unique tickers: {len(tickers)}")
    n_to_fetch = sum(1 for t in tickers if t not in cache)
    print(f"Cache hits: {len(tickers) - n_to_fetch}  fetching: {n_to_fetch}")

    fetched = 0
    for ticker in tickers:
        if ticker not in cache:
            fetch_market(ticker, cache)
            fetched += 1
            if fetched % 25 == 0:
                save_cache(cache)
                time.sleep(0.2)  # be polite
    save_cache(cache)

    # Now compute per-entry P&L using the cached market state.
    for row in rows:
        ticker = row["ticker"]
        side = row["side"]
        try:
            contracts = int(row["contracts"])
            entry_yes = int(row["price_cents"])
        except (TypeError, ValueError):
            continue
        # arb-leg rows record price_cents as that side's price, not the YES price.
        # Normalize: if row is from an arb leg (model_source endswith "_arb" or
        # contains "arb_leg"), and side=="no", then entry_yes = 100 - price_cents.
        src = (row.get("model_source") or "").lower()
        is_arb_leg = src.endswith("_arb") or src in {"arb_leg"} or "butterfly" in src
        if is_arb_leg and side == "no":
            entry_yes = 100 - entry_yes  # convert NO-leg price back to YES price

        market = cache.get(ticker, {})
        status, terminal_yes, is_terminal = classify_market(market)
        pnl = (
            per_contract_pnl(side, entry_yes, terminal_yes)
            if terminal_yes is not None else None
        )

        eff_yes = entry_yes if side == "yes" else (100 - entry_yes)
        pnls.append(EntryPnL(
            ticker=ticker,
            side=side,
            contracts=contracts,
            entry_yes_cents=entry_yes,
            fair_value=float(row.get("fair_value") or 0),
            confidence=float(row.get("confidence") or 0),
            edge=float(row.get("edge") or 0),
            model_source=row.get("model_source") or "",
            category=category_of(ticker),
            bucket=cost_bucket(eff_yes),
            status=status,
            terminal_yes_cents=terminal_yes,
            pnl_per_contract=pnl,
            is_terminal=is_terminal,
        ))
    return pnls


# ── Aggregation & report ──────────────────────────────────────────────────────

def pct(num: int, denom: int) -> str:
    return f"{100*num/denom:5.1f}%" if denom else "  --"


def report(pnls: list[EntryPnL]) -> None:
    total = len(pnls)
    resolved = [p for p in pnls if p.is_terminal]
    open_ = [p for p in pnls if not p.is_terminal and p.pnl_per_contract is not None]
    unknown = [p for p in pnls if p.pnl_per_contract is None]

    print("\n=== COVERAGE ===")
    print(f"  total entries:        {total}")
    print(f"  terminal (resolved):  {len(resolved):4d}  {pct(len(resolved), total)}")
    print(f"  MTM (still open):     {len(open_):4d}  {pct(len(open_), total)}")
    print(f"  unknown / no quote:   {len(unknown):4d}  {pct(len(unknown), total)}")

    # ── Per-category, terminal vs MTM ─────────────────────────────────────────
    print("\n=== CATEGORY TOTALS ===")
    print(f"  source        n   contracts   pnl_total¢   pnl_per_contract¢")

    def aggregate(group: list[EntryPnL], label: str) -> None:
        by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for p in group:
            if p.pnl_per_contract is None:
                continue
            stats = by_cat[p.category]
            stats[0] += 1
            stats[1] += p.contracts
            stats[2] += p.pnl_per_contract * p.contracts
        for cat, (n, contracts, total_c) in sorted(by_cat.items(),
                                                   key=lambda x: -x[1][2]):
            avg = total_c / max(1, contracts)
            print(f"  {label:8s} {cat:8s}  {n:5d}  {contracts:9d}  "
                  f"{total_c:+11d}  {avg:+8.2f}")

    aggregate(resolved, "TERM")
    aggregate(open_,    "MTM")
    aggregate(resolved + open_, "ALL")

    # ── Per (category, side, bucket) — terminal+MTM combined ──────────────────
    print("\n=== PER (CATEGORY, SIDE, EFF-COST BUCKET) — terminal+MTM combined ===")
    print(f"  {'category':10s} {'side':4s} {'bucket':15s}  n_entries  "
          f"contracts   total_c   ¢/contract")
    by_csb: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for p in resolved + open_:
        if p.pnl_per_contract is None:
            continue
        k = (p.category, p.side, p.bucket)
        by_csb[k][0] += 1
        by_csb[k][1] += p.contracts
        by_csb[k][2] += p.pnl_per_contract * p.contracts
    for (cat, side, bucket), (n, contracts, total_c) in sorted(by_csb.items()):
        avg = total_c / max(1, contracts)
        print(f"  {cat:10s} {side:4s} {bucket:15s}  {n:9d}  {contracts:9d}  "
              f"{total_c:+8d}  {avg:+8.2f}")

    # ── Just terminal (most defensible against Becker thesis) ─────────────────
    print("\n=== TERMINAL-ONLY by (category, side, bucket) ===")
    print(f"  {'category':10s} {'side':4s} {'bucket':15s}  n_entries  "
          f"contracts   total_c   ¢/contract")
    by_term: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for p in resolved:
        if p.pnl_per_contract is None:
            continue  # belt-and-suspenders; terminal always has a price
        k = (p.category, p.side, p.bucket)
        by_term[k][0] += 1
        by_term[k][1] += p.contracts
        by_term[k][2] += p.pnl_per_contract * p.contracts
    for (cat, side, bucket), (n, contracts, total_c) in sorted(by_term.items()):
        avg = total_c / max(1, contracts)
        print(f"  {cat:10s} {side:4s} {bucket:15s}  {n:9d}  {contracts:9d}  "
              f"{total_c:+8d}  {avg:+8.2f}")

    # ── Top model_sources, terminal+MTM ───────────────────────────────────────
    print("\n=== PER MODEL_SOURCE — terminal+MTM combined ===")
    print(f"  {'source':30s}  n   contracts    total_c   ¢/contract")
    by_src: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for p in resolved + open_:
        if p.pnl_per_contract is None:
            continue
        s = by_src[p.model_source or "(none)"]
        s[0] += 1
        s[1] += p.contracts
        s[2] += p.pnl_per_contract * p.contracts
    for src, (n, contracts, total_c) in sorted(by_src.items(),
                                               key=lambda x: -x[1][2]):
        avg = total_c / max(1, contracts)
        print(f"  {src:30s}  {n:5d}  {contracts:8d}  {total_c:+10d}  {avg:+8.2f}")

    # ── Becker-style price-bucket EV: side-level YES-cost ─────────────────────
    print("\n=== BECKER-STYLE EFF-COST EV (combined sides, terminal-only) ===")
    print(f"  {'bucket':15s}  n_entries  contracts   total_c    ¢/contract")
    by_b: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for p in resolved:
        if p.pnl_per_contract is None:
            continue
        s = by_b[p.bucket]
        s[0] += 1
        s[1] += p.contracts
        s[2] += p.pnl_per_contract * p.contracts
    for b, (n, contracts, total_c) in sorted(by_b.items()):
        avg = total_c / max(1, contracts)
        print(f"  {b:15s}  {n:9d}  {contracts:9d}  {total_c:+8d}    {avg:+8.2f}")

    # ── Save per-entry CSV for further analysis ───────────────────────────────
    out_csv = EDGEPULSE / "output" / "backtest_per_entry.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "ticker", "side", "contracts", "entry_yes_cents", "fair_value",
            "confidence", "edge", "model_source", "category", "bucket",
            "status", "terminal_yes_cents", "pnl_per_contract", "is_terminal",
        ])
        for p in pnls:
            w.writerow([
                p.ticker, p.side, p.contracts, p.entry_yes_cents,
                p.fair_value, p.confidence, p.edge, p.model_source,
                p.category, p.bucket, p.status, p.terminal_yes_cents,
                p.pnl_per_contract, int(p.is_terminal),
            ])
    print(f"\nWrote per-entry detail to {out_csv}")


if __name__ == "__main__":
    pnls = replay()
    report(pnls)
