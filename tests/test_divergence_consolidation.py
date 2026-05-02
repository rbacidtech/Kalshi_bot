"""
tests/test_divergence_consolidation.py — Smoke test for the consolidated
divergence pairing (Finding #3 in Agent 5's drift-source triage).

Background
----------
Before the consolidation, ep_exec.py's `_divergence_monitor_loop` did its own
hand-rolled CSV pairing with `entries[ticker] = row`, which silently OVERWROTE
prior entries for the same ticker. If a ticker had three independent
entry+exit cycles, the divergence monitor would pair only the last entry,
counting one trade where the canonical `_load_completed_trades` (strict FIFO)
would correctly pair three.

This test verifies the FIFO pairing semantics that the consolidated divergence
loop now relies on — three entry rows + three exit rows for the same ticker
must produce three completed trades, NOT one.

We also keep the original "3 entries + 1 exit" sub-case as documentation: with
strict FIFO, three entries against a single exit pair the EARLIEST entry only
(the other two entries remain open, so they don't appear in `completed`).
"""

import csv
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ep_resolution_db import _load_completed_trades


CSV_HEADERS = [
    "timestamp", "ticker", "meeting", "outcome", "side", "action",
    "contracts", "price_cents", "fair_value", "edge",
    "confidence", "model_source", "order_id", "mode",
]


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in CSV_HEADERS})


def _row(ts: datetime, ticker: str, action: str, *,
         price_cents: int, fair_value: float = 0.60,
         contracts: int = 5, side: str = "yes",
         mode: str = "live") -> dict:
    return {
        "timestamp":    ts.isoformat(),
        "ticker":       ticker,
        "meeting":      "KXFED-26JUN",
        "outcome":      "",
        "side":         side,
        "action":       action,
        "contracts":    contracts,
        "price_cents":  price_cents,
        "fair_value":   fair_value,
        "edge":         0.10,
        "confidence":   0.85,
        "model_source": "fred_anchor_4.25%",
        "order_id":     "test",
        "mode":         mode,
    }


def test_three_entries_one_exit_pairs_only_first_entry(tmp_path):
    """
    Three entries + one exit on the same ticker → strict FIFO pairs the
    earliest entry only. The OLD hand-rolled code would have lost the first
    two entries entirely (overwrite), then paired entry #3 with the lone exit.
    """
    csv_path = tmp_path / "trades_3e_1x.csv"
    base = datetime.now(timezone.utc) - timedelta(days=2)
    rows = [
        _row(base + timedelta(hours=0), "KXFED-26JUN-T4.25", "entry", price_cents=50),
        _row(base + timedelta(hours=1), "KXFED-26JUN-T4.25", "entry", price_cents=55),
        _row(base + timedelta(hours=2), "KXFED-26JUN-T4.25", "entry", price_cents=60),
        _row(base + timedelta(hours=3), "KXFED-26JUN-T4.25", "exit",  price_cents=70),
    ]
    _write_csv(csv_path, rows)

    since = datetime.now(timezone.utc) - timedelta(days=7)
    completed = _load_completed_trades(csv_path, since=since, mode="live")

    assert len(completed) == 1
    t = completed[0]
    # Strict FIFO → the FIRST entry (50¢) pairs with the lone exit (70¢).
    # The buggy old code would have paired the LAST entry (60¢).
    assert t["entry_price_cents"] == 50
    assert t["exit_price_cents"]  == 70
    assert t["pnl_cents"] == (70 - 50) * 5  # YES: (xp - ep) * contracts


def test_three_entries_three_exits_pairs_three_trades(tmp_path):
    """
    Three entries + three exits on the same ticker → strict FIFO produces
    three completed trades. The OLD code's `entries[ticker] = row` overwrite
    would have produced only one completed trade (the last entry vs. last
    exit), losing two trades.
    """
    csv_path = tmp_path / "trades_3e_3x.csv"
    base = datetime.now(timezone.utc) - timedelta(days=2)
    rows = [
        # Three open/close cycles on the same ticker
        _row(base + timedelta(hours=0),  "KXFED-26JUN-T4.25", "entry", price_cents=50),
        _row(base + timedelta(hours=1),  "KXFED-26JUN-T4.25", "exit",  price_cents=55),
        _row(base + timedelta(hours=2),  "KXFED-26JUN-T4.25", "entry", price_cents=60),
        _row(base + timedelta(hours=3),  "KXFED-26JUN-T4.25", "exit",  price_cents=65),
        _row(base + timedelta(hours=4),  "KXFED-26JUN-T4.25", "entry", price_cents=70),
        _row(base + timedelta(hours=5),  "KXFED-26JUN-T4.25", "exit",  price_cents=75),
    ]
    _write_csv(csv_path, rows)

    since = datetime.now(timezone.utc) - timedelta(days=7)
    completed = _load_completed_trades(csv_path, since=since, mode="live")

    assert len(completed) == 3, f"FIFO must produce 3 trades, got {len(completed)}"
    # Each trade is +5¢ × 5 contracts = 25¢ realized
    pnls = sorted(t["pnl_cents"] for t in completed)
    assert pnls == [25, 25, 25]


def test_old_entry_with_recent_exit_is_paired(tmp_path):
    """
    The OLD hand-rolled loop applied the 7d cutoff to every CSV row, so an
    entry 8d old whose exit was 1d old got dropped and the trade vanished.
    With the consolidated helper, the cutoff is applied to the EXIT timestamp
    only — the trade is correctly included.
    """
    csv_path = tmp_path / "trades_old_entry.csv"
    now = datetime.now(timezone.utc)
    rows = [
        _row(now - timedelta(days=8), "KXFED-26JUN-T4.25", "entry", price_cents=40),
        _row(now - timedelta(days=1), "KXFED-26JUN-T4.25", "exit",  price_cents=55),
    ]
    _write_csv(csv_path, rows)

    since = now - timedelta(days=7)
    completed = _load_completed_trades(csv_path, since=since, mode="live")

    assert len(completed) == 1
    assert completed[0]["entry_price_cents"] == 40
    assert completed[0]["exit_price_cents"]  == 55


def test_fair_value_cents_surfaced_for_divergence_loop(tmp_path):
    """
    The divergence loop needs entry-row fair_value (in cents) to compute
    expected P&L. Verify _load_completed_trades populates fair_value_cents
    from the entry row, normalising the 0-1 fraction to cents.
    """
    csv_path = tmp_path / "trades_fv.csv"
    base = datetime.now(timezone.utc) - timedelta(days=1)
    rows = [
        _row(base + timedelta(hours=0), "KXFED-26JUN-T4.25", "entry",
             price_cents=50, fair_value=0.72),
        _row(base + timedelta(hours=1), "KXFED-26JUN-T4.25", "exit",
             price_cents=60, fair_value=0.50),  # exit fv ignored
    ]
    _write_csv(csv_path, rows)

    since = datetime.now(timezone.utc) - timedelta(days=7)
    completed = _load_completed_trades(csv_path, since=since, mode="live")
    assert len(completed) == 1
    # Entry fair_value 0.72 × 100 = 72.0
    assert completed[0]["fair_value_cents"] == 72.0
