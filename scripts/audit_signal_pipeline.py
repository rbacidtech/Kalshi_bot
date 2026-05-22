"""
audit_signal_pipeline.py — End-to-end signal pipeline audit.

Walks the signal pipeline for a configurable time window and reports two
adjacent views the operator can correlate by eye:

  STREAM VIEW (ep:signals + ep:executions)
    Per model_source: PUBLISHED, FILLED, REJECTED-by-reason
    Ground truth — what actually reached the Redis bus.

  EMIT VIEW (kalshi_bot.jsonl)
    Per scanner: EMITTED count from INFO log lines
    Surfaces scanners that emit but get killed before reaching the stream
    (the gap detected on 2026-05-22 between phase 2 longshot scanners and
    ep:signals via the override_min_confidence=0.80 cap).

Usage:
    python3 scripts/audit_signal_pipeline.py [--minutes 30]

Exit code is always 0 — this is diagnostic, not pass/fail.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

JSONL = Path("/root/EdgePulse/output/logs/kalshi_bot.jsonl")
SOCK = "/run/redis/redis.sock"

# Patterns that match scanner INFO log emissions in kalshi_bot.jsonl.
# Each entry yields (canonical_name, count) from a regex match.
EMIT_PATTERNS = [
    # GDP/GDPNow nowcast scanners — each call emits per-threshold signals
    (re.compile(r"^GDP scan: (\d+) signals \(nowcast=([\d.]+)%\)"),     "gdpnow (multi)"),
    # FOMC / weather / sport / economic / crypto / sports — directional
    (re.compile(r"^FOMC directional: (\d+) signal"),                    "fomc_directional"),
    (re.compile(r"^Weather scanner: (\d+) signal"),                     "weather_directional"),
    (re.compile(r"^Economic: (\d+) signal"),                            "economic"),
    (re.compile(r"^Sports: (\d+) signal"),                              "sports"),
    (re.compile(r"^Crypto price scan: (\d+) signals from"),             "crypto_price"),
    # H2H sum-to-1 + phase 2 arbs (verdict §3)
    (re.compile(r"^H2H sum-to-1 ARB: (\d+) opportunit"),                "h2h_sum_to_1_arb"),
    (re.compile(r"^Spread monotonicity: (\d+) signals"),                "spread_monotonicity"),
    (re.compile(r"^Total monotonicity: (\d+) signals"),                 "total_monotonicity"),
    (re.compile(r"^NFL prop yardage monot: (\d+) signals"),             "nfl_prop_yardage_monot"),
    (re.compile(r"^Crypto threshold monot: (\d+) signals"),             "crypto_threshold_monot"),
    (re.compile(r"^A2 ARB:"),                                           "a2_cross_market_arb"),
    # Phase 2 longshot template — model_source IS the scanner name
    (re.compile(r"^([a-z_]+_longshot): (\d+) signals \(maker-priced\)"), "<longshot>"),
    (re.compile(r"^a1_mention_no: (\d+) signals"),                      "a1_mention_no"),
    (re.compile(r"^weather_longshot: (\d+) signals"),                   "weather_longshot"),
    # Other scanners published directly (not through fetch_signals_async)
    (re.compile(r"^Polymarket: (\d+) divergence signal"),               "polymarket_divergence"),
    (re.compile(r"^PredictIt divergence: (\d+) signal"),                "predictit_divergence"),
    (re.compile(r"^Cross-meeting coherence: (\d+) signal"),             "cross_meeting_coherence"),
    (re.compile(r"^Election\+metaculus: (\d+) signal"),                 "election_metaculus"),
    (re.compile(r"^BLS pre-position: (\d+) strangle"),                  "bls_preposition"),
    (re.compile(r"^Calendar decay: (\d+) signal"),                      "calendar_decay"),
    (re.compile(r"^Earnings: (\d+) signal"),                            "earnings"),
    (re.compile(r"^Intel: published (\d+) BTC signal"),                 "btc_mean_reversion"),
]


def _parse_ts(ts_str: str) -> float | None:
    """Parse a jsonl ISO timestamp ('2026-05-22T01:46:23') to unix seconds (UTC)."""
    if not ts_str or len(ts_str) < 19:
        return None
    try:
        return datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return None


def _scan_jsonl(cutoff: float) -> tuple[collections.Counter, int]:
    """Return (emits_by_scanner, untraceable_info_lines) within window."""
    emits = collections.Counter()
    untraceable = 0
    if not JSONL.exists():
        return emits, 0
    with JSONL.open() as f:
        for ln in f:
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("level") not in ("info", "warning"):
                continue
            ts = _parse_ts(rec.get("timestamp", ""))
            if ts is None or ts < cutoff:
                continue
            ev = rec.get("event", "")
            # Skip non-emit lines that happen to contain "signal"
            if not ev or ("signal" not in ev.lower() and "opportunit" not in ev.lower()):
                continue
            if ev.startswith("OB filter:") or ev.startswith("OB dropped"):
                continue
            matched = False
            for pat, canon in EMIT_PATTERNS:
                m = pat.match(ev)
                if not m:
                    continue
                if canon == "<longshot>":
                    name = m.group(1)
                    cnt = int(m.group(2))
                else:
                    # First numeric capture, if any
                    try:
                        cnt = int(m.group(1)) if m.lastindex else 0
                    except (ValueError, IndexError):
                        cnt = 0
                    name = canon
                emits[name] += cnt
                matched = True
                break
            if not matched:
                # Heuristic: lines with "N signal(s)" we couldn't classify
                if re.search(r"\b\d+ signal", ev):
                    untraceable += 1
    return emits, untraceable


def _read_stream(stream: str, cutoff_ms: int) -> list[dict]:
    """XRANGE the stream from cutoff to now via redis-cli; parse JSON payloads."""
    try:
        out = subprocess.check_output(
            ["redis-cli", "-s", SOCK, "xrange", stream, str(cutoff_ms), "+"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []
    payloads = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return payloads


def _stream_view(window_min: int) -> tuple[dict, int, int]:
    """Build (by_model_source → counters dict, total_published, total_executed)."""
    cutoff_ms = int((time.time() - window_min * 60) * 1000)
    signals = _read_stream("ep:signals", cutoff_ms)
    executions = _read_stream("ep:executions", cutoff_ms)

    # signal_id → model_source
    sig_id_to_ms = {}
    pub_by_ms = collections.Counter()
    for s in signals:
        ms = s.get("model_source") or s.get("strategy") or "<unknown>"
        sig_id_to_ms[s.get("signal_id")] = ms
        pub_by_ms[ms] += 1

    # Execution outcomes joined to model_source via signal_id
    exec_counts = collections.defaultdict(lambda: collections.Counter())
    for e in executions:
        sig_id = e.get("signal_id")
        ms = sig_id_to_ms.get(sig_id, "<unknown-signal>")
        status = e.get("status", "")
        if status == "rejected":
            exec_counts[ms][e.get("reject_reason", "REJECTED")] += 1
        elif status in ("filled", "partial_filled"):
            exec_counts[ms]["FILLED"] += 1
        else:
            exec_counts[ms][status or "OTHER"] += 1

    rows = {}
    for ms, n_pub in pub_by_ms.items():
        rows[ms] = {"PUBLISHED": n_pub, **exec_counts[ms]}
    return rows, len(signals), len(executions)


def _format_stream_table(rows: dict) -> str:
    if not rows:
        return "  (no signals in window)\n"
    # Collect all observed exec-outcome keys
    outcomes = set()
    for r in rows.values():
        outcomes.update(r.keys())
    outcomes.discard("PUBLISHED")
    outcome_cols = sorted(outcomes)
    cols = ["PUBLISHED"] + outcome_cols
    name_w = max(28, max(len(k) for k in rows))
    col_w = max(9, max(len(c) for c in cols))
    header = f"  {'model_source':<{name_w}}  " + "  ".join(f"{c:>{col_w}}" for c in cols)
    lines = [header, "  " + "-" * (len(header) - 2)]
    for ms in sorted(rows, key=lambda k: -rows[k].get("PUBLISHED", 0)):
        r = rows[ms]
        cells = [f"{r.get(c, 0):>{col_w}}" for c in cols]
        lines.append(f"  {ms:<{name_w}}  " + "  ".join(cells))
    return "\n".join(lines) + "\n"


def _format_emit_table(emits: collections.Counter, stream_rows: dict) -> str:
    if not emits:
        return "  (no scanner emits in window)\n"
    name_w = max(28, max(len(k) for k in emits))
    header = f"  {'scanner':<{name_w}}  {'EMITTED':>8}  {'PUBLISHED':>9}  GAP"
    lines = [header, "  " + "-" * (len(header) - 2)]
    for name in sorted(emits, key=lambda k: -emits[k]):
        n_emit = emits[name]
        # Crude match: stream's model_source may equal scanner name OR be a sibling
        n_pub = stream_rows.get(name, {}).get("PUBLISHED", 0)
        # Heuristic: if exact name has no published, try the GDPNow split
        if n_pub == 0 and name == "gdpnow (multi)":
            n_pub = sum(v.get("PUBLISHED", 0) for k, v in stream_rows.items()
                        if k.startswith("gdpnow_"))
        gap = ""
        if n_emit and n_pub == 0:
            gap = "100% killed pre-stream"
        elif n_emit and n_pub > 0 and n_pub < n_emit:
            pct = round(100 * (1 - n_pub / n_emit))
            if pct >= 30:
                gap = f"{pct}% killed pre-stream"
        lines.append(f"  {name:<{name_w}}  {n_emit:>8}  {n_pub:>9}  {gap}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[1])
    ap.add_argument("--minutes", type=int, default=30,
                    help="Window in minutes (default: 30)")
    args = ap.parse_args()
    cutoff = time.time() - args.minutes * 60

    now_utc = datetime.now(timezone.utc)
    start_utc = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    print(f"Signal pipeline audit — last {args.minutes} min  "
          f"({start_utc.strftime('%Y-%m-%d %H:%M')} → "
          f"{now_utc.strftime('%H:%M')} UTC)")
    print("=" * 78)

    stream_rows, n_sig, n_exec = _stream_view(args.minutes)
    print(f"\nSTREAM VIEW — {n_sig} signals, {n_exec} executions\n")
    print(_format_stream_table(stream_rows))

    emits, untraceable = _scan_jsonl(cutoff)
    print(f"EMIT VIEW — {sum(emits.values())} emitted signals across {len(emits)} scanners\n")
    print(_format_emit_table(emits, stream_rows))

    if untraceable:
        print(f"  [{untraceable} INFO line(s) with 'signal' pattern did not match any "
              f"emit regex — extend EMIT_PATTERNS in this script]")

    total_pub = sum(r.get("PUBLISHED", 0) for r in stream_rows.values())
    total_filled = sum(r.get("FILLED", 0) for r in stream_rows.values())
    fill_rate = (100 * total_filled / total_pub) if total_pub else 0
    print(f"\nSUMMARY")
    print(f"  total published:  {total_pub}")
    print(f"  total filled:     {total_filled}  ({fill_rate:.1f}% of published)")
    if stream_rows:
        # Most common reject reason across all model_sources
        reject_totals = collections.Counter()
        for r in stream_rows.values():
            for k, v in r.items():
                if k not in ("PUBLISHED", "FILLED"):
                    reject_totals[k] += v
        if reject_totals:
            top = reject_totals.most_common(3)
            print(f"  top reject reasons:")
            for reason, n in top:
                print(f"    {n:6d}  {reason}")


if __name__ == "__main__":
    main()
