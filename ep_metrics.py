"""
ep_metrics.py — Prometheus instrumentation for EdgePulse.

Exposes metrics at http://:<METRICS_PORT>/metrics and /health on the same port.

All call sites are backward-compatible. New helpers added for the expanded catalog.
"""

import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

try:
    from prometheus_client import (
        Counter, Gauge, Histogram, Info,
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
    def info(self, *_, **__): pass


def _noop() -> "_Noop":
    return _Noop()


class EdgePulseMetrics:
    """
    Thread-safe Prometheus metrics wrapper.

    If prometheus_client is not installed all calls are no-ops so the bot
    runs normally — install with: pip install prometheus-client
    """

    def __init__(self) -> None:
        self._started    = False
        self._health: dict = {"status": "starting"}
        self._session_pnl = 0.0

        if not _HAVE_PROMETHEUS:
            log.warning("prometheus_client not installed — metrics disabled.")
            self._null = True
            return

        self._null = False

        # ── Signal flow ───────────────────────────────────────────────────────
        self.signals_total = Counter(
            "edgepulse_signals_published_total",
            "Signals published to ep:signals",
            ["strategy", "asset_class", "side"],
        )
        self.executions_total = Counter(
            "edgepulse_executions_total",
            "Execution reports",
            ["status", "reason", "asset_class", "strategy"],
        )

        # ── Latencies ─────────────────────────────────────────────────────────
        self.signal_fill_latency = Histogram(
            "edgepulse_signal_fill_latency_seconds",
            "Time from signal emission to order fill",
            ["asset_class"],
            buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 30],
        )
        self.signal_stream_lag = Histogram(
            "edgepulse_signal_stream_lag_seconds",
            "Time signal spends in ep:signals stream before Exec reads it",
            ["asset_class"],
            buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30],
        )
        self.risk_processing_time = Histogram(
            "edgepulse_risk_processing_seconds",
            "Time to run _process_signal (risk gates + sizing, excluding exchange)",
            ["asset_class"],
            buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2],
        )
        self.redis_op_latency = Histogram(
            "edgepulse_redis_op_seconds",
            "Redis operation latency",
            ["op"],
            buckets=[0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5],
        )
        self.exchange_api_latency = Histogram(
            "edgepulse_exchange_api_seconds",
            "Exchange HTTP request latency",
            ["exchange", "endpoint", "outcome"],
            buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
        )
        self.cycle_duration = Histogram(
            "edgepulse_cycle_duration_seconds",
            "Duration of each Intel main-loop cycle",
            buckets=[0.5, 1, 2, 5, 10, 30, 60, 120, 300],
        )

        # ── Position / balance state ──────────────────────────────────────────
        self.open_positions = Gauge(
            "edgepulse_open_positions_total",
            "Open positions count",
            ["asset_class"],
        )
        self.balance_cents = Gauge(
            "edgepulse_balance_cents",
            "Balance in cents",
            ["asset_class"],
        )
        self.exposure_cents = Gauge(
            "edgepulse_exposure_cents",
            "Current exposure in cents",
            ["asset_class", "category"],
        )
        self.pnl_cents = Gauge(
            "edgepulse_daily_pnl_cents",
            "Realized P&L today in cents",
            ["asset_class"],
        )
        self.daily_pnl_pct = Gauge(
            "edgepulse_daily_pnl_pct",
            "Realized P&L today as fraction of starting balance",
            ["asset_class"],
        )
        self.position_without_price = Gauge(
            "edgepulse_position_without_price",
            "Positions with no fresh price in ep:prices",
        )

        # ── BTC state ─────────────────────────────────────────────────────────
        self.btc_price = Gauge("edgepulse_btc_spot_price", "BTC spot price USD")
        self.btc_rsi   = Gauge("edgepulse_btc_rsi_14",     "BTC RSI(14)")
        self.btc_z_score = Gauge("edgepulse_btc_zscore",   "BTC price z-score vs 20-period mean")
        self.btc_bb_upper = Gauge("edgepulse_btc_bb_upper", "Bollinger upper band")
        self.btc_bb_mid   = Gauge("edgepulse_btc_bb_mid",   "Bollinger mid band")
        self.btc_bb_lower = Gauge("edgepulse_btc_bb_lower", "Bollinger lower band")

        # ── Data source health ────────────────────────────────────────────────
        self.data_source_health = Gauge(
            "edgepulse_data_source_health",
            "1=up, 0=down",
            ["source"],
        )
        self.data_source_last_success = Gauge(
            "edgepulse_data_source_last_success_timestamp",
            "Unix timestamp of last successful fetch",
            ["source"],
        )

        # ── Stream state ──────────────────────────────────────────────────────
        self.stream_length = Gauge(
            "edgepulse_stream_length",
            "Redis stream length",
            ["stream"],
        )
        self.stream_lag = Gauge(
            "edgepulse_stream_lag_seconds",
            "Age of oldest unconsumed signal in stream",
            ["stream"],
        )

        # ── Invariant / correctness ───────────────────────────────────────────
        self.invariant_violations = Counter(
            "edgepulse_invariant_violations_total",
            "Invariant assertion failures",
            ["invariant"],
        )
        self.stale_prices_skipped = Counter(
            "edgepulse_stale_prices_skipped_total",
            "Exit checks skipped due to stale price",
            ["ticker"],
        )

        # ── Risk gate decisions ───────────────────────────────────────────────
        self.risk_gate_outcomes = Counter(
            "edgepulse_risk_gate_total",
            "Risk gate decisions",
            ["gate", "outcome"],
        )
        self.kelly_contracts = Histogram(
            "edgepulse_kelly_contracts",
            "Kelly-sized contracts per signal",
            ["asset_class"],
            buckets=[0, 1, 2, 3, 5, 10, 25, 50, 100, 250, 500],
        )

        # ── Circuit breaker ───────────────────────────────────────────────────
        self.circuit_breaker_state = Gauge(
            "edgepulse_circuit_breaker_state",
            "0=closed, 1=half-open, 2=open",
            ["breaker"],
        )
        self.circuit_breaker_failures = Counter(
            "edgepulse_circuit_breaker_failures_total",
            "Consecutive failures observed by breaker",
            ["breaker"],
        )

        # ── Build / deploy info ───────────────────────────────────────────────
        try:
            git_sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            git_sha = "unknown"
        self._build_info = Gauge(
            "edgepulse_build_info",
            "Always 1; labels encode build metadata",
            ["git_sha", "deployed_at"],
        )
        self._build_info.labels(
            git_sha     = git_sha,
            deployed_at = str(int(time.time())),
        ).set(1)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_health(self, checks: dict) -> None:
        """Update the /health response dict (called from async loop, read by HTTP thread)."""
        self._health = checks

    def start(self, port: int = 9091) -> None:
        """Start the HTTP server on :port serving /metrics and /health (idempotent)."""
        if self._null or self._started:
            return

        metrics_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/health"):
                    health = metrics_ref._health
                    body   = json.dumps(health).encode()
                    ok     = health.get("status") in ("ok", "starting")
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
                pass

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

    # ── Signal / execution ────────────────────────────────────────────────────

    def signal_published(self, asset_class: str, strategy: str, side: str) -> None:
        if self._null:
            return
        self.signals_total.labels(
            strategy=strategy, asset_class=asset_class, side=side,
        ).inc()

    def execution_received(
        self,
        status:     str,
        asset_class: str,
        reason:     str = "",
        strategy:   str = "",
    ) -> None:
        if self._null:
            return
        self.executions_total.labels(
            status=status, reason=reason,
            asset_class=asset_class, strategy=strategy,
        ).inc()

    def record_fill_latency(self, asset_class: str, latency_s: float) -> None:
        if self._null:
            return
        self.signal_fill_latency.labels(asset_class=asset_class).observe(latency_s)

    def record_stream_lag(self, asset_class: str, lag_s: float) -> None:
        if self._null:
            return
        self.signal_stream_lag.labels(asset_class=asset_class).observe(lag_s)

    def record_risk_processing(self, asset_class: str, elapsed_s: float) -> None:
        if self._null:
            return
        self.risk_processing_time.labels(asset_class=asset_class).observe(elapsed_s)

    def record_exchange_latency(
        self, exchange: str, endpoint: str, outcome: str, latency_s: float,
    ) -> None:
        if self._null:
            return
        self.exchange_api_latency.labels(
            exchange=exchange, endpoint=endpoint, outcome=outcome,
        ).observe(latency_s)

    # ── Balance / position ────────────────────────────────────────────────────

    def update_balance(self, cents: int, asset_class: str = "kalshi") -> None:
        if self._null:
            return
        self.balance_cents.labels(asset_class=asset_class).set(cents)

    def update_exposure(
        self, cents: int, asset_class: str = "kalshi", category: str = "all",
    ) -> None:
        if self._null:
            return
        self.exposure_cents.labels(asset_class=asset_class, category=category).set(cents)

    def update_positions(self, count: int, asset_class: str = "kalshi") -> None:
        if self._null:
            return
        self.open_positions.labels(asset_class=asset_class).set(count)

    def add_pnl(self, edge_captured: float, asset_class: str = "kalshi") -> None:
        """Accumulate realized edge on fills."""
        if self._null:
            return
        self._session_pnl += edge_captured
        self.pnl_cents.labels(asset_class=asset_class).set(self._session_pnl)

    def set_pnl(self, cents: float, asset_class: str = "kalshi") -> None:
        if self._null:
            return
        self.pnl_cents.labels(asset_class=asset_class).set(cents)

    def set_pnl_pct(self, pct: float, asset_class: str = "kalshi") -> None:
        if self._null:
            return
        self.daily_pnl_pct.labels(asset_class=asset_class).set(pct)

    # ── BTC ───────────────────────────────────────────────────────────────────

    def update_btc(
        self,
        price:    Optional[float] = None,
        rsi:      Optional[float] = None,
        z:        Optional[float] = None,
        bb_upper: Optional[float] = None,
        bb_mid:   Optional[float] = None,
        bb_lower: Optional[float] = None,
    ) -> None:
        if self._null:
            return
        if price    is not None: self.btc_price.set(price)
        if rsi      is not None: self.btc_rsi.set(rsi)
        if z        is not None: self.btc_z_score.set(z)
        if bb_upper is not None: self.btc_bb_upper.set(bb_upper)
        if bb_mid   is not None: self.btc_bb_mid.set(bb_mid)
        if bb_lower is not None: self.btc_bb_lower.set(bb_lower)

    # ── Data sources ──────────────────────────────────────────────────────────

    def record_source_up(self, source: str) -> None:
        if self._null:
            return
        self.data_source_health.labels(source=source).set(1)
        self.data_source_last_success.labels(source=source).set(time.time())

    def record_source_down(self, source: str) -> None:
        if self._null:
            return
        self.data_source_health.labels(source=source).set(0)

    # ── Stream state ──────────────────────────────────────────────────────────

    def update_stream(self, stream: str, length: int, lag_s: float = 0.0) -> None:
        if self._null:
            return
        self.stream_length.labels(stream=stream).set(length)
        self.stream_lag.labels(stream=stream).set(lag_s)

    # ── Invariants / risk gates ───────────────────────────────────────────────

    def record_stale_price_skip(self, ticker: str) -> None:
        if self._null:
            return
        self.stale_prices_skipped.labels(ticker=ticker).inc()

    def record_invariant_violation(self, invariant: str) -> None:
        if self._null:
            return
        self.invariant_violations.labels(invariant=invariant).inc()

    def record_risk_gate(self, gate: str, outcome: str) -> None:
        if self._null:
            return
        self.risk_gate_outcomes.labels(gate=gate, outcome=outcome).inc()

    def record_kelly(self, asset_class: str, contracts: int) -> None:
        if self._null:
            return
        self.kelly_contracts.labels(asset_class=asset_class).observe(contracts)

    # ── Circuit breakers ──────────────────────────────────────────────────────

    def update_circuit_breaker(self, breaker: str, state: int) -> None:
        """state: 0=closed, 1=half-open, 2=open"""
        if self._null:
            return
        self.circuit_breaker_state.labels(breaker=breaker).set(state)

    def record_circuit_breaker_failure(self, breaker: str) -> None:
        if self._null:
            return
        self.circuit_breaker_failures.labels(breaker=breaker).inc()

    # ── Cycle timing ──────────────────────────────────────────────────────────

    def observe_cycle(self, elapsed_s: float) -> None:
        if self._null:
            return
        self.cycle_duration.observe(elapsed_s)

    @contextmanager
    def cycle_timer(self):
        if self._null:
            yield
            return
        with self.cycle_duration.time():
            yield


# Module-level singleton — import from anywhere
metrics = EdgePulseMetrics()
