"""
ep_config.py — Shared runtime config, Redis key namespace, and sys.path bootstrap.

Import this BEFORE any kalshi_bot.* imports in every ep_*.py file — the
sys.path.insert calls here make the kalshi_bot package importable from any
working directory.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── sys.path bootstrap ────────────────────────────────────────────────────────
# After flattening, kalshi_bot/ and ep_*.py are siblings in the same directory.
# A single insert covers both — no parent traversal needed.
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))          # /root/EdgePulse — kalshi_bot + ep_* modules

import kalshi_bot.config as cfg  # noqa: E402  (must follow sys.path setup)

# ── Runtime config (environment-driven) ──────────────────────────────────────
MODE          = os.getenv("MODE", "intel").lower()           # "intel" | "exec"
NODE_ID       = os.getenv("NODE_ID", f"{MODE}-{os.uname().nodename}")
REDIS_URL     = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SIGNAL_TTL    = int(os.getenv("EP_SIGNAL_TTL_MS",  "60000"))   # 60 s
STREAM_BLOCK  = int(os.getenv("EP_STREAM_BLOCK_MS", "5000"))    # 5 s blocking read
EXIT_INTERVAL = int(os.getenv("EP_EXIT_INTERVAL_S", "60"))      # seconds between exit checks

# ── Redis key namespace ───────────────────────────────────────────────────────
EP_SIGNALS    = "ep:signals"      # STREAM  Intel → Exec
EP_EXECUTIONS = "ep:executions"   # STREAM  Exec  → Intel
EP_POSITIONS  = "ep:positions"    # HASH    ticker → position JSON (Exec writes)
EP_PRICES     = "ep:prices"       # HASH    ticker → price JSON   (Intel writes)
EP_BALANCE    = "ep:balance"      # HASH    node_id → balance JSON
EP_SYSTEM     = "ep:system"       # STREAM  lifecycle events
EP_CONFIG     = "ep:config"       # HASH    runtime overrides (ops + LLM)
EP_HEALTH     = "ep:health"       # HASH    node_id → health JSON  (Intel writes)

EXEC_GROUP    = "exec-consumers"   # consumer group on ep:signals
INTEL_GROUP   = "intel-consumers"  # consumer group on ep:executions

log = logging.getLogger("edgepulse")


def validate() -> None:
    """Check critical config at startup; warn on misconfiguration."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    if "localhost" in REDIS_URL and NODE_ID.startswith("exec"):
        raise ValueError(
            f"NODE_ID=exec but REDIS_URL points to localhost — "
            f"exec node must point to intel node Redis"
        )
    if "@" not in REDIS_URL and "localhost" not in REDIS_URL and "127.0.0.1" not in REDIS_URL:
        raise ValueError(
            "REDIS_URL must include authentication credentials (redis://:password@host:port). "
            "Unauthenticated remote Redis is a security risk."
        )
    if MODE not in ("intel", "exec", "both"):
        raise ValueError(f"MODE must be 'intel' or 'exec', got {MODE!r}")
    if not os.getenv("KALSHI_API_KEY_ID"):
        _log.warning("KALSHI_API_KEY_ID not set — will run in paper mode only")
    if not os.getenv("FRED_API_KEY"):
        _log.warning("FRED_API_KEY not set — using demo key (rate-limited to 120 req/day)")
    # NOTE: FRED does not support Authorization header auth — the key must be passed
    # as an api_key query parameter. URLs containing the key are never logged.


validate()
