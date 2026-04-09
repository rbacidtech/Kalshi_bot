"""
config.py — Validated configuration for the FOMC-focused bot.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ── Credentials ───────────────────────────────────────────────────────────────
API_KEY_ID       = os.getenv("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = Path(os.getenv("KALSHI_PRIVATE_KEY_PATH", "private_key.pem"))

# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER_TRADE = os.getenv("KALSHI_PAPER_TRADE", "true").lower() == "true"

_DEFAULT_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if PAPER_TRADE
    else "https://api.elections.kalshi.com/trade-api/v2"
)
BASE_URL = os.getenv("KALSHI_BASE_URL", "").strip() or _DEFAULT_URL

# ── Strategy ──────────────────────────────────────────────────────────────────
# Raised from 0.05 to 0.10 — minimum edge must exceed typical fee (7¢) plus margin
EDGE_THRESHOLD   = float(os.getenv("KALSHI_EDGE_THRESHOLD", "0.10"))
MAX_CONTRACTS    = int(os.getenv("KALSHI_MAX_CONTRACTS", "10"))
POLL_INTERVAL    = int(os.getenv("KALSHI_POLL_INTERVAL", "120"))   # 2 min default for FOMC
MIN_CONFIDENCE   = float(os.getenv("KALSHI_MIN_CONFIDENCE", "0.60"))

# ── Paths ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("output")
TRADES_CSV = OUTPUT_DIR / "trades.csv"

# ── HTTP ──────────────────────────────────────────────────────────────────────
HTTP_TIMEOUT  = int(os.getenv("KALSHI_HTTP_TIMEOUT", "10"))
MAX_RETRIES   = int(os.getenv("KALSHI_MAX_RETRIES", "3"))
RETRY_BACKOFF = float(os.getenv("KALSHI_RETRY_BACKOFF", "2.0"))
CONCURRENCY   = int(os.getenv("KALSHI_CONCURRENCY", "10"))   # fewer needed for FOMC-only

# ── Risk ──────────────────────────────────────────────────────────────────────
KELLY_FRACTION        = float(os.getenv("KALSHI_KELLY_FRACTION", "0.25"))
MAX_MARKET_EXPOSURE   = float(os.getenv("KALSHI_MAX_MARKET_EXPOSURE", "0.05"))
MAX_TOTAL_EXPOSURE    = float(os.getenv("KALSHI_MAX_TOTAL_EXPOSURE", "0.30"))  # tighter: FOMC only
DAILY_DRAWDOWN_LIMIT  = float(os.getenv("KALSHI_DAILY_DRAWDOWN_LIMIT", "0.10"))
MAX_SPREAD_CENTS      = int(os.getenv("KALSHI_MAX_SPREAD_CENTS", "10"))        # tighter for FOMC
FEE_CENTS             = int(os.getenv("KALSHI_FEE_CENTS", "7"))

# ── Exit management ───────────────────────────────────────────────────────────
TAKE_PROFIT_CENTS    = int(os.getenv("KALSHI_TAKE_PROFIT_CENTS", "20"))
STOP_LOSS_CENTS      = int(os.getenv("KALSHI_STOP_LOSS_CENTS", "15"))
HOURS_BEFORE_CLOSE   = float(os.getenv("KALSHI_HOURS_BEFORE_CLOSE", "24.0"))


def validate():
    errors = []

    if not PAPER_TRADE:
        if not API_KEY_ID:
            errors.append("KALSHI_API_KEY_ID is required for live trading.")
        if not PRIVATE_KEY_PATH.exists():
            errors.append(f"Private key not found at '{PRIVATE_KEY_PATH}'.")

    if EDGE_THRESHOLD < 0.08:
        errors.append(
            f"KALSHI_EDGE_THRESHOLD={EDGE_THRESHOLD} is below 0.08. "
            "With 7¢ fees, edges below 8¢ are likely unprofitable."
        )
    if MAX_CONTRACTS < 1:
        errors.append(f"KALSHI_MAX_CONTRACTS must be >= 1.")
    if POLL_INTERVAL < 30:
        errors.append(f"KALSHI_POLL_INTERVAL must be >= 30s for FOMC markets.")
    if not 0 < MIN_CONFIDENCE <= 1:
        errors.append(f"KALSHI_MIN_CONFIDENCE must be in (0, 1].")

    if errors:
        for e in errors:
            log.error("Config error: %s", e)
        raise ValueError("Invalid configuration — fix the errors above and restart.")
