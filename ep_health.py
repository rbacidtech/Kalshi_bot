"""
ep_health.py — Data source health tracker and circuit breakers for EdgePulse.

Tracks which external data sources succeeded/failed each cycle, with staleness
detection, consecutive failure counts, and per-source circuit breakers.

Usage:
    from ep_health import record_success, record_failure, get_health_summary
    from ep_health import get_breaker

    record_success("kalshi_ws")
    record_failure("cme_fedwatch", "403 Forbidden")

    cb = get_breaker("fred_vix")
    result = await cb.call(fetch_fn, arg1, arg2)

    summary = get_health_summary()
    # {"overall": "healthy", "sources": {...}}

Legacy singleton API (backwards-compatible):
    from ep_health import health
    health.mark_ok("kalshi_ws")
    health.mark_fail("cme_fedwatch", "403 Forbidden")
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ep_config import log


# ── DataSource descriptor ────────────────────────────────────────────────────

@dataclass
class DataSource:
    """Descriptor for a single data source, with staleness and failure tracking."""
    name:          str
    stale_seconds: int
    critical:      bool
    last_success:  float = 0.0    # epoch seconds; 0 means never seen
    failure_count: int   = 0      # consecutive failures since last success
    last_error:    str   = ""     # most recent error string (truncated to 200 chars)

    def age_seconds(self) -> float:
        """Seconds since last successful update. Very large if never seen."""
        if self.last_success == 0.0:
            return float("inf")
        return time.time() - self.last_success

    def is_stale(self) -> bool:
        return self.age_seconds() > self.stale_seconds

    def status(self) -> str:
        """Return 'ok', 'stale', or 'failed'."""
        if self.failure_count > 0 and self.is_stale():
            return "failed"
        if self.is_stale():
            return "stale"
        return "ok"


# ── Master source registry ───────────────────────────────────────────────────

_SOURCES: Dict[str, DataSource] = {
    # ── Kalshi exchange ──────────────────────────────────────────────────────
    "kalshi_ws":     DataSource("kalshi_ws",     stale_seconds=120,         critical=True),
    "kalshi_rest":   DataSource("kalshi_rest",   stale_seconds=300,         critical=True),

    # ── FRED macro series ────────────────────────────────────────────────────
    "fred_dfedtaru": DataSource("fred_dfedtaru", stale_seconds=86400,       critical=False),
    "fred_core_cpi": DataSource("fred_core_cpi", stale_seconds=86400,       critical=False),
    "fred_pce":      DataSource("fred_pce",      stale_seconds=86400,       critical=False),
    "fred_icsa":     DataSource("fred_icsa",     stale_seconds=86400,       critical=False),  # weekly
    "fred_t10y2y":   DataSource("fred_t10y2y",   stale_seconds=86400,       critical=False),
    "fred_t5yifr":   DataSource("fred_t5yifr",   stale_seconds=86400,       critical=False),
    "fred_unrate":   DataSource("fred_unrate",   stale_seconds=86400 * 7,   critical=False),  # monthly
    "fred_vix":      DataSource("fred_vix",      stale_seconds=86400,       critical=False),
    "fred_dgs10":    DataSource("fred_dgs10",    stale_seconds=86400,       critical=False),

    # ── CME / rate markets ───────────────────────────────────────────────────
    "cme_fedwatch":  DataSource("cme_fedwatch",  stale_seconds=600,         critical=False),
    "cme_sofr_sr1":  DataSource("cme_sofr_sr1",  stale_seconds=600,         critical=False),

    # ── GDPNow ──────────────────────────────────────────────────────────────
    "gdpnow":        DataSource("gdpnow",        stale_seconds=604800,      critical=False),

    # ── Cross-market prediction markets ─────────────────────────────────────
    "predictit":     DataSource("predictit",     stale_seconds=600,         critical=False),
    "spd":           DataSource("spd",           stale_seconds=86400 * 7,   critical=False),

    # ── BLS releases ────────────────────────────────────────────────────────
    "bls_cpi":       DataSource("bls_cpi",       stale_seconds=86400 * 35,  critical=False),  # monthly
    "bls_nfp":       DataSource("bls_nfp",       stale_seconds=86400 * 35,  critical=False),

    # ── Infrastructure ───────────────────────────────────────────────────────
    # Intel loop is 120s; 150s threshold avoids false-stale between cycles
    "redis":         DataSource("redis",         stale_seconds=150,         critical=True),
    "exec_heartbeat": DataSource("exec_heartbeat", stale_seconds=180,       critical=True),
}


# ── Module-level API ─────────────────────────────────────────────────────────

def record_success(name: str) -> None:
    """Mark source as healthy, reset failure count."""
    src = _SOURCES.get(name)
    if src is None:
        # Auto-register unknown sources as non-critical with 1-hour staleness.
        _SOURCES[name] = DataSource(name, stale_seconds=3600, critical=False)
        src = _SOURCES[name]
    src.last_success  = time.time()
    src.failure_count = 0
    src.last_error    = ""


def record_failure(name: str, error: str = "") -> None:
    """Increment consecutive failure count and update last_error."""
    src = _SOURCES.get(name)
    if src is None:
        _SOURCES[name] = DataSource(name, stale_seconds=3600, critical=False)
        src = _SOURCES[name]
    src.failure_count += 1
    src.last_error     = str(error)[:200]
    if src.critical:
        log.warning("Data source DOWN (critical): %s — failures=%d — %s",
                    name, src.failure_count, str(error)[:80])


def get_failure_count(name: str) -> int:
    """Return consecutive failure count for a source (0 if unknown)."""
    src = _SOURCES.get(name)
    return src.failure_count if src else 0


def get_health_summary() -> dict:
    """
    Return a dict describing overall system health and per-source details.

    Format::
        {
            "overall": "healthy" | "degraded" | "critical",
            "sources": {
                "<name>": {
                    "status":   "ok" | "stale" | "failed",
                    "age_s":    float,
                    "failures": int,
                    "error":    str,
                }
            }
        }

    Severity rules:
      - "critical"  — any critical source is stale or has failures > 0
      - "degraded"  — any non-critical source has failures > 3
      - "healthy"   — otherwise
    """
    overall    = "healthy"
    sources_out: dict = {}

    for name, src in _SOURCES.items():
        age    = src.age_seconds()
        status = src.status()
        sources_out[name] = {
            "status":   status,
            "age_s":    round(age, 1) if age != float("inf") else None,
            "failures": src.failure_count,
            "error":    src.last_error,
        }

        if src.critical and status in ("stale", "failed"):
            overall = "critical"
        elif overall != "critical" and not src.critical and src.failure_count > 3:
            overall = "degraded"

    return {"overall": overall, "sources": sources_out}


# ── Circuit Breaker ──────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Wraps an async data fetch function. After `threshold` consecutive failures,
    opens the circuit and returns None immediately until `reset_seconds` elapse.

    This prevents hammering unavailable APIs and burning timeout budgets.
    """

    def __init__(self, name: str, threshold: int = 3, reset_seconds: int = 300):
        self.name          = name
        self.threshold     = threshold
        self.reset_seconds = reset_seconds
        self._failures     = 0
        self._opened_at    = 0.0

    async def call(self, coro_factory, *args, **kwargs):
        """
        Call coro_factory(*args, **kwargs) with circuit breaker protection.
        Returns the coroutine's result, or None if the circuit is open.
        """
        now = time.time()
        if self._failures >= self.threshold:
            if now - self._opened_at < self.reset_seconds:
                log.debug(
                    "Circuit open for %s — skipping fetch (%ds remaining)",
                    self.name,
                    int(self.reset_seconds - (now - self._opened_at)),
                )
                return None
            else:
                log.info("Circuit reset for %s after %ds", self.name, self.reset_seconds)
                self._failures = 0

        try:
            result = await coro_factory(*args, **kwargs)
            if result is not None:
                self._failures = 0
                record_success(self.name)
            return result
        except Exception as exc:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = now
                log.warning(
                    "Circuit opened for %s after %d failures: %s",
                    self.name, self._failures, exc,
                )
                # Fire-and-forget Telegram alert on first open (exactly at threshold)
                if self._failures == self.threshold:
                    try:
                        from ep_telegram import telegram as _telegram
                        asyncio.ensure_future(
                            _telegram.send_circuit_breaker_alert(
                                self.name, self._failures
                            )
                        )
                    except Exception:
                        pass
            record_failure(self.name, str(exc))
            return None


# ── Per-source circuit breakers ──────────────────────────────────────────────

_BREAKERS: Dict[str, CircuitBreaker] = {
    "cme_fedwatch":  CircuitBreaker("cme_fedwatch",  threshold=3, reset_seconds=300),
    "cme_sofr_sr1":  CircuitBreaker("cme_sofr_sr1",  threshold=3, reset_seconds=300),
    "fred_core_cpi": CircuitBreaker("fred_core_cpi", threshold=5, reset_seconds=3600),
    "fred_pce":      CircuitBreaker("fred_pce",      threshold=5, reset_seconds=3600),
    "fred_icsa":     CircuitBreaker("fred_icsa",     threshold=5, reset_seconds=3600),
    "fred_t10y2y":   CircuitBreaker("fred_t10y2y",   threshold=5, reset_seconds=3600),
    "fred_t5yifr":   CircuitBreaker("fred_t5yifr",   threshold=5, reset_seconds=3600),
    "fred_unrate":   CircuitBreaker("fred_unrate",   threshold=5, reset_seconds=3600),
    "fred_vix":      CircuitBreaker("fred_vix",      threshold=5, reset_seconds=3600),
    "predictit":     CircuitBreaker("predictit",     threshold=3, reset_seconds=600),
    "spd":           CircuitBreaker("spd",           threshold=2, reset_seconds=86400),
    "bls_releases":  CircuitBreaker("bls_releases",  threshold=3, reset_seconds=300),
}


def get_breaker(name: str) -> CircuitBreaker:
    """Return the named circuit breaker, or a new one with default settings."""
    return _BREAKERS.get(name, CircuitBreaker(name))


# ── Legacy singleton API (backwards-compatible) ──────────────────────────────

class _LegacyHealthProxy:
    """
    Thin shim so old call-sites using `health.mark_ok()`/`health.mark_fail()`
    continue to work without modification.  New code should call the module-level
    functions directly.
    """

    # ── Old-style dict-based status (kept for to_dict / summary / log_cycle) ─

    def __init__(self) -> None:
        self._status: Dict[str, dict] = {}

    def mark_ok(self, source: str, detail: str = "") -> None:
        record_success(source)
        self._status[source] = {
            "ok":      True,
            "last_ok": time.time(),
            "error":   None,
            "detail":  detail,
        }

    def mark_fail(self, source: str, error: str, warn: bool = False) -> None:
        record_failure(source, error)
        prev    = self._status.get(source, {})
        was_ok  = prev.get("ok", True)
        self._status[source] = {
            "ok":      False,
            "last_ok": prev.get("last_ok"),
            "error":   str(error)[:120],
            "detail":  "",
        }
        if warn or was_ok:
            log.warning("Data source DOWN: %s — %s", source, str(error)[:80])

    def is_ok(self, source: str) -> bool:
        return self._status.get(source, {}).get("ok", False)

    def all_critical_ok(self) -> bool:
        return all(
            self._status.get(s, {}).get("ok", False)
            for s, src in _SOURCES.items()
            if src.critical
        )

    def get_down(self) -> list:
        return [s for s, v in self._status.items() if not v["ok"]]

    def get_up(self) -> list:
        return [s for s, v in self._status.items() if v["ok"]]

    def summary(self) -> str:
        up     = self.get_up()
        down   = self.get_down()
        unseen = [s for s in _SOURCES if s not in self._status]
        parts  = []
        if up:
            parts.append(f"UP={len(up)}")
        if down:
            parts.append(f"DOWN={down}")
        if unseen:
            parts.append(f"UNSEEN={len(unseen)}")
        return "  ".join(parts) or "no sources tracked yet"

    def to_dict(self) -> dict:
        return {
            source: {
                "ok":      info["ok"],
                "last_ok": info.get("last_ok"),
                "error":   info.get("error"),
            }
            for source, info in self._status.items()
        }

    def log_cycle_summary(self) -> None:
        down = self.get_down()
        if not down:
            log.debug("Data sources: all OK (%d tracked)", len(self._status))
        else:
            critical_down = [s for s in down if _SOURCES.get(s) and _SOURCES[s].critical]
            if critical_down:
                log.warning("Data sources: critical sources DOWN: %s", critical_down)
            else:
                log.info("Data sources: optional sources down: %s", down)


# Module-level singleton — kept for backwards compatibility
health = _LegacyHealthProxy()
