"""
ep_metrics.py — Prometheus instrumentation for EdgePulse.

Exposes metrics at http://:<METRICS_PORT>/metrics for Prometheus to scrape.

Metrics:
  edgepulse_signals_total{asset_class, strategy, side}   Counter
  edgepulse_executions_total{status, asset_class}         Counter
  edgepulse_balance_cents                                 Gauge
  edgepulse_pnl_cents                                     Gauge
  edgepulse_open_positions                                Gauge
  edgepulse_btc_price_usd                                 Gauge
  edgepulse_btc_rsi                                       Gauge
  edgepulse_btc_z_score                                   Gauge
  edgepulse_cycle_duration_seconds                        Histogram

Usage (Intel node, called once at startup then per-cycle):
    from ep_metrics import metrics
    metrics.start(port=9091)
    metrics.signal_published("btc_spot", "btc_mr", "buy")
    metrics.update_btc(84000.0, rsi=32.1, z=-1.8)
    with metrics.cycle_timer():
        ...main loop body...
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

try:
    from prometheus_client import (
        Counter, Gauge, Histogram,
        generate_latest, CONTENT_TYPE_LATEST,
    )
    _HAVE_PROMETHEUS = True
except ImportError:
    _HAVE_PROMETHEUS = False

from ep_config import log


class _Noop:
    """Silent stand-in when prometheus_client is not installed."""
    def labels(self, **_): return self
    def inc(self, *_, **__): pass
    def set(self, *_, **__): pass
    def observe(self, *_, **__): pass


class EdgePulseMetrics:
    """
    Thread-safe Prometheus metrics wrapper.

    If prometheus_client is not installed all calls are no-ops so the bot
    runs normally — install with: pip install prometheus-client
    """

    def __init__(self) -> None:
        self._started = False
        self._health: dict = {"status": "starting"}

        if not _HAVE_PROMETHEUS:
            log.warning("prometheus_client not installed — metrics disabled. "
                        "Install with: pip install prometheus-client")
            self._null = True
            return

        self._null = False

        self.signals_total = Counter(
            "edgepulse_signals_total",
            "Signals published to the Redis edge-bus",
            ["asset_class", "strategy", "side"],
        )
        self.executions_total = Counter(
            "edgepulse_executions_total",
            "Execution reports drained from ep:executions",
            ["status", "asset_class"],
        )
        self.balance_cents = Gauge(
            "edgepulse_balance_cents",
            "Current account balance in cents (Intel balance + paper default)",
        )
        self.pnl_cents = Gauge(
            "edgepulse_pnl_cents",
            "Session realized P&L in edge-cents (sum of edge_captured on fills)",
        )
        self.open_positions = Gauge(
            "edgepulse_open_positions",
            "Number of open positions tracked in ep:positions Redis hash",
        )
        self.btc_price = Gauge(
            "edgepulse_btc_price_usd",
            "Current BTC spot price from Coinbase (USD)",
        )
        self.btc_rsi = Gauge(
            "edgepulse_btc_rsi",
            "Current BTC RSI-14 computed by ep_btc.py",
        )
        self.btc_z_score = Gauge(
            "edgepulse_btc_z_score",
            "Current BTC rolling z-score vs Bollinger mid computed by ep_btc.py",
        )
        self.cycle_duration = Histogram(
            "edgepulse_cycle_duration_seconds",
            "Duration of each Intel main-loop cycle",
            buckets=[0.5, 1, 2, 5, 10, 30, 60, 120, 300],
        )
        self._session_pnl = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_health(self, checks: dict) -> None:
        """Update the /health response dict (called from async loop, read by HTTP thread)."""
        self._health = checks

    def start(self, port: int = 9091) -> None:
        """Start the HTTP server serving /metrics and /health (idempotent).

        A port conflict (e.g. rapid systemd restart before the OS releases the
        socket) is logged as a warning but does NOT crash the process — metrics
        are non-critical and the trading loop must continue regardless.
        """
        if self._null or self._started:
            return

        metrics_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/health"):
                    health = metrics_ref._health
                    body   = json.dumps(health).encode()
                    ok     = health.get("status") == "ok"
                    self.send_response(200 if ok else 503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/metrics":
                    output = generate_latest()
                    self.send_response(200)
                    self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                    self.send_header("Content-Length", str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, fmt, *args):
                pass  # suppress per-request access logs

        try:
            server = ThreadingHTTPServer(("", port), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._started = True
            log.info("Metrics/health server listening on :%d  (/metrics  /health)", port)
        except OSError as exc:
            log.warning(
                "Metrics server could not bind to port %d (%s) — "
                "metrics will be unavailable this session but trading continues.",
                port, exc,
            )

    # ── Per-event helpers ─────────────────────────────────────────────────────

    def signal_published(self, asset_class: str, strategy: str, side: str) -> None:
        if self._null:
            return
        self.signals_total.labels(
            asset_class=asset_class, strategy=strategy, side=side,
        ).inc()

    def execution_received(self, status: str, asset_class: str) -> None:
        if self._null:
            return
        self.executions_total.labels(status=status, asset_class=asset_class).inc()

    # ── Per-cycle helpers ─────────────────────────────────────────────────────

    def update_balance(self, cents: int) -> None:
        if self._null:
            return
        self.balance_cents.set(cents)

    def add_pnl(self, edge_captured: float) -> None:
        """Accumulate realized edge on fills (in units of cents × contracts)."""
        if self._null:
            return
        self._session_pnl += edge_captured
        self.pnl_cents.set(self._session_pnl)

    def update_positions(self, count: int) -> None:
        if self._null:
            return
        self.open_positions.set(count)

    def update_btc(
        self,
        price: Optional[float] = None,
        rsi:   Optional[float] = None,
        z:     Optional[float] = None,
    ) -> None:
        if self._null:
            return
        if price is not None:
            self.btc_price.set(price)
        if rsi is not None:
            self.btc_rsi.set(rsi)
        if z is not None:
            self.btc_z_score.set(z)

    def observe_cycle(self, elapsed_s: float) -> None:
        """Record one cycle's wall-clock duration to the histogram."""
        if self._null:
            return
        self.cycle_duration.observe(elapsed_s)

    @contextmanager
    def cycle_timer(self):
        """Context manager that records cycle wall-time to the histogram."""
        if self._null:
            yield
            return
        with self.cycle_duration.time():
            yield


# Module-level singleton — import from anywhere
metrics = EdgePulseMetrics()
