"""
models/cache.py — Thread-safe in-memory TTL cache for external API responses.

Why this matters for speed:
  The bot scans every market every cycle (default 60s). Without caching,
  every cycle would re-fetch NOAA forecasts, Yahoo Finance quotes, and
  Fed futures data — even though those data sources update far less
  frequently than Kalshi's own order books.

  Cache TTLs are tuned to data freshness:
    - Yahoo Finance (SPY futures, VIX):  60s   — updates every minute
    - CME FedWatch probabilities:        300s  — updates intraday but slowly
    - NOAA forecasts:                    1800s — NWS updates every 1-6 hours
    - Resolved market filter:            3600s — no point re-checking

Usage:
    cache = TTLCache()
    cache.set("noaa:NYC:precip", 0.34, ttl=1800)
    val = cache.get("noaa:NYC:precip")   # returns 0.34 or None if expired
"""

import time
import threading
import logging
from typing import Any

log = logging.getLogger(__name__)


class TTLCache:
    """
    Simple thread-safe in-memory key-value cache with per-entry TTL.

    Uses a single lock for all operations. For the scale of this bot
    (hundreds of keys, sub-millisecond access) this is faster than
    more complex concurrent structures and avoids stale-read bugs.
    """

    def __init__(self):
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expiry_ts)
        self._lock  = threading.Lock()

    def get(self, key: str) -> Any | None:
        """
        Return cached value if present and not expired, else None.
        Expired entries are lazily deleted on access.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if time.monotonic() > expiry:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        """Store a value with a TTL in seconds."""
        with self._lock:
            self._store[key] = (value, time.monotonic() + ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def purge_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (_, exp) in self._store.items() if now > exp]
            for k in expired:
                del self._store[k]
        if expired:
            log.debug("Cache: purged %d expired entries.", len(expired))
        return len(expired)

    def stats(self) -> dict:
        with self._lock:
            now   = time.monotonic()
            total = len(self._store)
            live  = sum(1 for _, (_, exp) in self._store.items() if now <= exp)
        return {"total_keys": total, "live_keys": live, "expired_keys": total - live}


# Module-level singleton shared across all models
_cache = TTLCache()


def get_cache() -> TTLCache:
    """Return the shared cache instance."""
    return _cache
