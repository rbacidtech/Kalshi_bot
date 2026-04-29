"""
backtest_weather_blend.py — counterfactual on weather forecast blending.

For every terminal weather entry in output/backtest_per_entry.csv, refetch
Open-Meteo historical forecasts (GFS + ECMWF separately) for the target
date.  Recompute fair_value under four rules and score each against the
actual Kalshi resolution via Brier loss:

  Rule 0 (BASELINE):  the bot's original fair_value as recorded at entry
                      (effectively gfs + nws_hourly, since ECMWF was
                      silently broken until 2026-04-29's ifs04→ifs025 fix)
  Rule A (GFS_ONLY):  refit using only Open-Meteo GFS forecast
  Rule B (ECMWF_ONLY): refit using only Open-Meteo ECMWF (ifs025) forecast
  Rule C (GFS+ECMWF): blended GFS (w=1.0) + ECMWF (w=1.3)

LIMITATIONS this can NOT test:
  • NWS hourly XOR NWS daily — NWS doesn't expose a clean historical-forecast
    JSON API. The original "blend all 4 vs XOR" question can't be cleanly
    answered with public data.
  • This script answers the closest empirical proxy: does adding ECMWF
    (currently re-enabled) materially improve calibration vs a single-source
    forecast, on our actual realized terminal outcomes?

Brier score is per-entry (pred - actual)^2 where actual ∈ {0, 1}; the side
flips it for NO entries (we want P(this-side-wins) calibration). Lower is
better.

Output: prints per-rule mean Brier, per-bucket comparison, and writes
output/weather_blend_comparison.csv.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

EDGEPULSE = Path("/root/EdgePulse")
PER_ENTRY = EDGEPULSE / "output" / "backtest_per_entry.csv"
OUT_CSV   = EDGEPULSE / "output" / "weather_blend_comparison.csv"
HF_BASE   = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Mirrors strategy.py WEATHER_SERIES (subset used in our tickers)
WEATHER_CITIES = {
    "KXHIGHNY":  {"lat": 40.7128, "lon": -74.0060,  "tz": "America/New_York",    "type": "high_temp"},
    "KXLOWNY":   {"lat": 40.7128, "lon": -74.0060,  "tz": "America/New_York",    "type": "low_temp"},
    "KXHIGHLA":  {"lat": 34.0522, "lon": -118.2437, "tz": "America/Los_Angeles", "type": "high_temp"},
    "KXHIGHCHI": {"lat": 41.8781, "lon": -87.6298,  "tz": "America/Chicago",     "type": "high_temp"},
    "KXHIGHDC":  {"lat": 38.9072, "lon": -77.0369,  "tz": "America/New_York",    "type": "high_temp"},
}

_MONTH = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def parse_target_date(ticker: str) -> str | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)", ticker)
    if not m:
        return None
    mo = _MONTH.get(m.group(2))
    if not mo:
        return None
    return f"20{m.group(1)}-{mo:02d}-{int(m.group(3)):02d}"


def parse_strike(ticker: str) -> tuple[str, float | None, float | None]:
    """
    Returns (strike_type, floor, cap) for a weather ticker.
      KXHIGHCHI-26APR29-T54     → ("greater", 54, None)
      KXHIGHCHI-26APR29-B54.5   → ("between", 54.5, 55.5)  (assume 1°F band)
    Kalshi B-series bands are typically 1°F wide.
    """
    m = re.search(r"-([TB])([\d.]+)$", ticker)
    if not m:
        return "greater", None, None
    kind, num = m.group(1), float(m.group(2))
    if kind == "T":
        return "greater", num, None
    return "between", num, num + 1.0


def temp_prob_above(forecast: float, threshold: float, sigma: float = 2.5) -> float:
    z = (threshold - forecast) / sigma
    return 0.5 * (1.0 - math.erf(z / math.sqrt(2)))


def _norminv(p: float) -> float:
    """Inverse standard-normal CDF (Acklam approximation, sufficient precision)."""
    if p <= 0.0 or p >= 1.0:
        return float("nan")
    a = (-3.969683028665376e+01,  2.209460984245205e+02,
         -2.759285104469687e+02,  1.383577518672690e+02,
         -3.066479806614716e+01,  2.506628277459239e+00)
    b = (-5.447609879822406e+01,  1.615858368580409e+02,
         -1.556989798598866e+02,  6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00)
    d = ( 7.784695709041462e-03,  3.224671290700398e-01,
          2.445134137142996e+00,  3.754408661907416e+00)
    plow  = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q*q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def implied_mean_from_fv(fv: float, strike: float, sigma: float) -> float | None:
    """Invert P(temp > strike) = fv to recover mean_temp. Returns None if fv is degenerate."""
    if fv <= 0.001 or fv >= 0.999:
        return None
    # P(T > strike) = fv  →  Φ((mean-strike)/σ) = fv  →  (mean-strike)/σ = Φ⁻¹(fv)
    z = _norminv(fv)
    return strike + z * sigma


def fair_value_for(side_temp: float, ticker: str) -> float | None:
    """Compute fair_value (P(YES)) for a weather ticker given a forecast temp."""
    strike_type, floor, cap = parse_strike(ticker)
    if floor is None:
        return None
    if strike_type == "greater":
        return temp_prob_above(side_temp, floor)
    if strike_type == "between":
        # P(floor <= temp <= cap)  = P(>floor) - P(>cap)
        if cap is None:
            return None
        return max(0.0, temp_prob_above(side_temp, floor) - temp_prob_above(side_temp, cap))
    return None


def fetch_hf(lat: float, lon: float, tz: str, target: str,
             models: str | None = None, cache: dict | None = None) -> dict | None:
    """Open-Meteo historical-forecast for a single date. Returns {'high', 'low'} or None."""
    key = (round(lat, 4), round(lon, 4), tz, target, models or "default")
    if cache is not None and key in cache:
        return cache[key]
    params = {
        "latitude":         f"{lat}",
        "longitude":        f"{lon}",
        "start_date":       target,
        "end_date":         target,
        "daily":            "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone":         tz,
    }
    if models:
        params["models"] = models
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{HF_BASE}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError:
        if cache is not None:
            cache[key] = None
        return None
    except Exception:
        if cache is not None:
            cache[key] = None
        return None
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows  = daily.get("temperature_2m_min") or []
    if not times or times[0] != target:
        if cache is not None:
            cache[key] = None
        return None
    h = highs[0] if highs else None
    l = lows[0]  if lows  else None
    if h is None and l is None:
        result = None
    else:
        result = {"high": h, "low": l}
    if cache is not None:
        cache[key] = result
    return result


def brier_for_side(prob_yes: float, side: str, resolved_yes: bool) -> float:
    """Brier-loss for the position side. Lower is better."""
    p_win = prob_yes if side == "yes" else (1.0 - prob_yes)
    actual_win = 1.0 if (resolved_yes if side == "yes" else not resolved_yes) else 0.0
    return (p_win - actual_win) ** 2


def main() -> None:
    if not PER_ENTRY.exists():
        print(f"ERROR: {PER_ENTRY} missing — run scripts/backtest_terminal.py first")
        sys.exit(1)

    rows: list[dict] = []
    with PER_ENTRY.open() as f:
        for r in csv.DictReader(f):
            if r["category"] != "weather":
                continue
            if int(r["is_terminal"]) != 1:
                continue
            rows.append(r)

    print(f"Terminal weather entries: {len(rows)}")
    series_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        for prefix in WEATHER_CITIES:
            if r["ticker"].startswith(prefix):
                series_counts[prefix] += 1
                break
    print(f"Series breakdown: {dict(series_counts)}")

    # Pre-compute unique (city_prefix, target_date) combos for batch fetch.
    fetch_keys: set[tuple[str, str]] = set()
    for r in rows:
        for prefix in WEATHER_CITIES:
            if r["ticker"].startswith(prefix):
                tgt = parse_target_date(r["ticker"])
                if tgt:
                    fetch_keys.add((prefix, tgt))
                break
    print(f"Unique (city, target_date) combos to fetch: {len(fetch_keys)}")

    # Fetch GFS (default) and ECMWF for each combo, with cache.
    cache_default: dict = {}
    cache_ecmwf:   dict = {}
    forecasts: dict[tuple[str, str], dict] = {}  # (prefix, date) -> {"gfs": {...}, "ecmwf": {...}}

    fetch_fail = 0
    for i, (prefix, tgt) in enumerate(sorted(fetch_keys), 1):
        cfg = WEATHER_CITIES[prefix]
        gfs   = fetch_hf(cfg["lat"], cfg["lon"], cfg["tz"], tgt, None,           cache_default)
        time.sleep(0.05)
        ecmwf = fetch_hf(cfg["lat"], cfg["lon"], cfg["tz"], tgt, "ecmwf_ifs025", cache_ecmwf)
        time.sleep(0.05)
        forecasts[(prefix, tgt)] = {"gfs": gfs, "ecmwf": ecmwf}
        if gfs is None and ecmwf is None:
            fetch_fail += 1
        if i % 25 == 0:
            print(f"  [{i}/{len(fetch_keys)}] fetched {prefix} {tgt}")
    print(f"Fetch complete. Failures: {fetch_fail}")

    # Compute Brier per rule per entry.
    # all3_t = "all-3 blend reconstructed from bot's recorded fv" — only valid
    # for T-series (greater) markets where the inverse normal is monotonic.
    rule_brier: dict[str, list[float]] = {
        "original": [], "gfs": [], "ecmwf": [], "blend": [], "all3_t": [],
    }
    rule_skipped = {"gfs": 0, "ecmwf": 0, "blend": 0, "all3_t": 0}
    out_rows: list[list] = []

    for r in rows:
        ticker = r["ticker"]
        side   = r["side"]
        resolved_yes = (r["status"] == "resolved_yes")
        original_fv  = float(r["fair_value"])
        prefix = next((p for p in WEATHER_CITIES if ticker.startswith(p)), None)
        tgt    = parse_target_date(ticker)
        if prefix is None or tgt is None:
            continue
        cfg     = WEATHER_CITIES[prefix]
        ttype   = cfg["type"]
        temp_key = "high" if ttype == "high_temp" else "low"

        f = forecasts.get((prefix, tgt), {})
        gfs   = f.get("gfs")
        ecmwf = f.get("ecmwf")
        gfs_t   = gfs[temp_key]   if gfs   and gfs.get(temp_key)   is not None else None
        ecmwf_t = ecmwf[temp_key] if ecmwf and ecmwf.get(temp_key) is not None else None

        # Rule 0: original recorded fair_value
        rule_brier["original"].append(brier_for_side(original_fv, side, resolved_yes))

        # Rule A: GFS only
        if gfs_t is not None:
            fv_a = fair_value_for(gfs_t, ticker)
            if fv_a is not None:
                rule_brier["gfs"].append(brier_for_side(fv_a, side, resolved_yes))
            else:
                rule_skipped["gfs"] += 1
        else:
            rule_skipped["gfs"] += 1

        # Rule B: ECMWF only
        if ecmwf_t is not None:
            fv_b = fair_value_for(ecmwf_t, ticker)
            if fv_b is not None:
                rule_brier["ecmwf"].append(brier_for_side(fv_b, side, resolved_yes))
            else:
                rule_skipped["ecmwf"] += 1
        else:
            rule_skipped["ecmwf"] += 1

        # Rule C: GFS+ECMWF blend
        if gfs_t is not None and ecmwf_t is not None:
            mean_t = (gfs_t * 1.0 + ecmwf_t * 1.3) / 2.3
            fv_c = fair_value_for(mean_t, ticker)
            if fv_c is not None:
                rule_brier["blend"].append(brier_for_side(fv_c, side, resolved_yes))
            else:
                rule_skipped["blend"] += 1
        else:
            rule_skipped["blend"] += 1

        # Rule D (all3_t): reverse-engineer the bot's implied 2-source mean from
        # original_fv, then add ECMWF on top to simulate "GFS + NWS-hourly +
        # ECMWF" (3-source). T-series only (between-series inverse is non-
        # monotonic and would need a 2D solver). Sigma assumed = 2.5°F day-of.
        strike_type, floor, _ = parse_strike(ticker)
        fv_all3 = None
        if (
            strike_type == "greater"
            and floor is not None
            and ecmwf_t is not None
        ):
            inferred_2src_mean = implied_mean_from_fv(original_fv, floor, sigma=2.5)
            if inferred_2src_mean is not None:
                # Bot's 2-source weight was 1.0 (GFS) + 1.2 (NWS-hourly) = 2.2.
                # Adding ECMWF (w=1.3) gives total weight 3.5.
                new_mean = (inferred_2src_mean * 2.2 + ecmwf_t * 1.3) / 3.5
                fv_all3 = temp_prob_above(new_mean, floor, sigma=2.5)
                rule_brier["all3_t"].append(brier_for_side(fv_all3, side, resolved_yes))
            else:
                rule_skipped["all3_t"] += 1
        else:
            rule_skipped["all3_t"] += 1

        out_rows.append([
            ticker, side, "yes" if resolved_yes else "no", original_fv,
            gfs_t, ecmwf_t,
            fair_value_for(gfs_t, ticker)   if gfs_t   is not None else None,
            fair_value_for(ecmwf_t, ticker) if ecmwf_t is not None else None,
            fair_value_for((gfs_t * 1.0 + ecmwf_t * 1.3) / 2.3, ticker)
                if (gfs_t is not None and ecmwf_t is not None) else None,
            fv_all3,
        ])

    print()
    print("=" * 72)
    print("BRIER LOSS (lower = better calibrated; range 0-1; coin-flip = 0.25)")
    print("=" * 72)
    for rule, scores in rule_brier.items():
        n = len(scores)
        if n == 0:
            print(f"  {rule:10s}  n=0  (no data)")
            continue
        mean = sum(scores) / n
        print(f"  {rule:10s}  n={n:4d}  brier={mean:.4f}  skipped={rule_skipped.get(rule, 0)}")

    print()
    print("Pairwise difference (rule - original; positive = worse than original):")
    base = rule_brier["original"]
    base_mean = sum(base) / len(base) if base else 0
    for rule in ("gfs", "ecmwf", "blend", "all3_t"):
        scores = rule_brier[rule]
        if not scores:
            continue
        m = sum(scores) / len(scores)
        # n won't match exactly across rules due to skipping; report as informational
        print(f"  {rule:10s}  brier={m:.4f}  Δ_vs_original={m - base_mean:+.4f}  ({'BETTER' if m < base_mean else 'WORSE'})")

    # For all3_t, also report against an APPLES-TO-APPLES original baseline
    # restricted to T-series entries only (since all3_t skips B-series).
    print()
    print("Apples-to-apples (T-series only, where all3_t is defined):")
    t_only_brier = []
    for r in rows:
        if parse_strike(r["ticker"])[0] != "greater":
            continue
        # Re-derive original brier for this T-series entry
        original_fv = float(r["fair_value"])
        side = r["side"]
        resolved_yes = (r["status"] == "resolved_yes")
        t_only_brier.append(brier_for_side(original_fv, side, resolved_yes))
    if t_only_brier and rule_brier["all3_t"]:
        t_orig = sum(t_only_brier) / len(t_only_brier)
        t_all3 = sum(rule_brier["all3_t"]) / len(rule_brier["all3_t"])
        print(f"  original (T only)  n={len(t_only_brier):3d}  brier={t_orig:.4f}")
        print(f"  all3_t  (T only)  n={len(rule_brier['all3_t']):3d}  brier={t_all3:.4f}  "
              f"Δ={t_all3 - t_orig:+.4f}  ({'BETTER' if t_all3 < t_orig else 'WORSE'})")

    # Save per-entry detail
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "ticker", "side", "resolved", "original_fv",
            "gfs_temp", "ecmwf_temp",
            "fv_gfs_only", "fv_ecmwf_only", "fv_blend", "fv_all3_t",
        ])
        for row in out_rows:
            w.writerow(row)
    print(f"\nWrote per-entry comparison to {OUT_CSV}")


if __name__ == "__main__":
    main()
