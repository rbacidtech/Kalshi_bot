"""
config.py — Validated configuration for the FOMC-focused bot.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def _getenv_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        log.error("Config: %s=%r is not an integer, using default %d", key, val, default)
        return default


def _getenv_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        log.error("Config: %s=%r is not a float, using default %f", key, val, default)
        return default


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
# Minimum edge must exceed typical fee (7¢) plus spread margin
EDGE_THRESHOLD   = _getenv_float("KALSHI_EDGE_THRESHOLD", 0.10)
MAX_CONTRACTS    = _getenv_int("KALSHI_MAX_CONTRACTS", 10)
POLL_INTERVAL    = _getenv_int("KALSHI_POLL_INTERVAL", 120)   # 2 min default for FOMC
MIN_CONFIDENCE   = _getenv_float("KALSHI_MIN_CONFIDENCE", 0.60)
# Gate for signals where the only source is a static fallback (FRED anchor, no futures data).
FALLBACK_ONLY_EDGE_THRESHOLD = _getenv_float("KALSHI_FALLBACK_ONLY_EDGE_THRESHOLD", 0.25)

# ── Paths ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("output")
TRADES_CSV = OUTPUT_DIR / "trades.csv"

# ── HTTP ──────────────────────────────────────────────────────────────────────
HTTP_TIMEOUT  = _getenv_int("KALSHI_HTTP_TIMEOUT", 10)
MAX_RETRIES   = _getenv_int("KALSHI_MAX_RETRIES", 3)
RETRY_BACKOFF = _getenv_float("KALSHI_RETRY_BACKOFF", 2.0)
CONCURRENCY   = _getenv_int("KALSHI_CONCURRENCY", 10)   # fewer needed for FOMC-only

# ── Risk ──────────────────────────────────────────────────────────────────────
KELLY_FRACTION        = _getenv_float("KALSHI_KELLY_FRACTION", 0.25)
MAX_MARKET_EXPOSURE   = _getenv_float("KALSHI_MAX_MARKET_EXPOSURE", 0.05)
MAX_TOTAL_EXPOSURE    = _getenv_float("KALSHI_MAX_TOTAL_EXPOSURE", 0.30)  # tighter: FOMC only
DAILY_DRAWDOWN_LIMIT  = _getenv_float("KALSHI_DAILY_DRAWDOWN_LIMIT", 0.10)
MAX_SPREAD_CENTS      = _getenv_int("KALSHI_MAX_SPREAD_CENTS", 10)        # tighter for FOMC
FEE_CENTS             = _getenv_int("KALSHI_FEE_CENTS", 7)

# ── Signal filtering ──────────────────────────────────────────────────────────
# Suppress KXFED YES signals where the market price is below this threshold.
# Data shows YES entries below 60¢ have ~11-13% win rate (avg -55¢ to -116¢/trade).
# YES entries above 60¢ are profitable; this gate targets the losing population only.
# Set KALSHI_MIN_YES_ENTRY_PRICE=0.0 to disable.
MIN_YES_ENTRY_PRICE  = _getenv_float("KALSHI_MIN_YES_ENTRY_PRICE", 0.60)

# ── Kelly calibration ──────────────────────────────────────────────────────────
# Minimum number of *terminal* trades (exit at 0¢ or 100¢) required before using
# the terminal-only Kelly calibration.  Below this count the full trade population
# is used as a fallback to avoid overfitting to a tiny sample.
MIN_KELLY_TRADES     = _getenv_int("KALSHI_MIN_KELLY_TRADES", 10)

# ── Exit management ───────────────────────────────────────────────────────────
TAKE_PROFIT_CENTS    = _getenv_int("KALSHI_TAKE_PROFIT_CENTS", 20)
STOP_LOSS_CENTS      = _getenv_int("KALSHI_STOP_LOSS_CENTS", 15)
HOURS_BEFORE_CLOSE   = _getenv_float("KALSHI_HOURS_BEFORE_CLOSE", 24.0)
TRAILING_STOP_CENTS  = _getenv_int("KALSHI_TRAILING_STOP_CENTS", 12)   # exit if profit retreats 12¢ from peak
# Max positions open for the same FOMC meeting date (prevents correlated overexposure).
# Example: with HOLD at 3.75%, T2.75/T3.00/T3.25 YES are all positively correlated.
MAX_POSITIONS_PER_MEETING = _getenv_int("KALSHI_MAX_POSITIONS_PER_MEETING", 4)


def validate():
    errors = []

    if not PAPER_TRADE:
        if not API_KEY_ID:
            errors.append("KALSHI_API_KEY_ID is required for live trading.")
        if not PRIVATE_KEY_PATH.exists():
            errors.append(f"Private key not found at '{PRIVATE_KEY_PATH}'.")

    if not 0.08 <= EDGE_THRESHOLD <= 0.50:
        errors.append(
            f"KALSHI_EDGE_THRESHOLD={EDGE_THRESHOLD} must be between 0.08 and 0.50. "
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
