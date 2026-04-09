"""
dashboard.py — Live browser dashboard served by Flask.

Runs as a background thread on http://localhost:5050 (configurable).
The main bot loop and the dashboard share the BotState object — the
dashboard just reads from it and serves it to the browser.

Uses Server-Sent Events (SSE) for live updates: the browser opens one
persistent HTTP connection and receives JSON pushes whenever BotState
changes. No WebSocket on the browser side, no polling, no page refreshes.

Open http://localhost:5050 while the bot is running to see:
  - Live FOMC market prices vs FedWatch fair values
  - Open positions with real-time unrealized P&L
  - Recent trades feed
  - Session stats (balance, P&L, cycles, drawdown)
  - Signal strength bars
  - Live equity curve (session P&L over time)
  - WebSocket connection status indicator
"""

import json
import time
import queue
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import BotState

log = logging.getLogger(__name__)


def start_dashboard(state: "BotState", host: str = "0.0.0.0", port: int = 5050):
    """
    Start the dashboard Flask server in a background daemon thread.
    Call once at bot startup.
    """
    try:
        from flask import Flask, Response, render_template_string
    except ImportError:
        log.warning("Flask not installed — dashboard disabled. Run: pip install flask")
        return

    app = Flask(__name__)

    # SSE subscriber queues — one per connected browser tab
    _sse_clients: list[queue.Queue] = []
    _sse_lock = threading.Lock()

    def push_to_clients(data: dict):
        """Push a JSON snapshot to all connected SSE clients."""
        payload = f"data: {json.dumps(data)}\n\n"
        with _sse_lock:
            dead = []
            for q in _sse_clients:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                _sse_clients.remove(q)

    # Subscribe to state changes
    def on_state_change(event_type, _data):
        if event_type in ("market_update", "trade", "balance",
                          "position_opened", "position_closed",
                          "signals", "ws_status"):
            push_to_clients(state.snapshot())

    state.subscribe(on_state_change)

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template_string(_DASHBOARD_HTML)

    @app.route("/api/state")
    def api_state():
        return json.dumps(state.snapshot()), 200, {"Content-Type": "application/json"}

    @app.route("/api/stream")
    def sse_stream():
        """Server-Sent Events endpoint — browser subscribes here for live updates."""
        client_q: queue.Queue = queue.Queue(maxsize=50)
        with _sse_lock:
            _sse_clients.append(client_q)

        def generate():
            # Send current state immediately on connect
            yield f"data: {json.dumps(state.snapshot())}\n\n"
            try:
                while True:
                    try:
                        data = client_q.get(timeout=30)
                        yield data
                    except queue.Empty:
                        yield ": heartbeat\n\n"  # keep connection alive
            except GeneratorExit:
                with _sse_lock:
                    if client_q in _sse_clients:
                        _sse_clients.remove(client_q)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Start server ──────────────────────────────────────────────────────────

    def run_server():
        import logging as _log
        _log.getLogger("werkzeug").setLevel(logging.WARNING)   # suppress Flask request logs
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    thread = threading.Thread(target=run_server, name="dashboard", daemon=True)
    thread.start()
    log.info("Dashboard running at http://localhost:%d", port)


# ── HTML/CSS/JS (single-file dashboard) ──────────────────────────────────────

_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi FOMC Bot</title>
<style>
  :root {
    --bg:      #0a0e1a;
    --surface: #111827;
    --border:  #1f2937;
    --text:    #e2e8f0;
    --muted:   #6b7280;
    --green:   #10b981;
    --red:     #ef4444;
    --yellow:  #f59e0b;
    --blue:    #3b82f6;
    --purple:  #8b5cf6;
    --font:    'IBM Plex Mono', 'Courier New', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: var(--font); font-size: 13px; line-height: 1.6;
    min-height: 100vh; padding: 16px;
  }
  /* ── Header ── */
  .header {
    display: flex; align-items: center; justify-content: space-between;
    padding-bottom: 12px; border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
  }
  .header h1 { font-size: 18px; letter-spacing: 0.05em; color: var(--blue); }
  .header .meta { display: flex; gap: 20px; color: var(--muted); font-size: 11px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: bold; letter-spacing: 0.08em;
  }
  .badge-paper  { background: #1d2d1a; color: var(--green); }
  .badge-live   { background: #2d1a1a; color: var(--red); }
  .badge-ws-on  { background: #1a2d2d; color: var(--green); }
  .badge-ws-off { background: #2d2d1a; color: var(--yellow); }

  /* ── Grid ── */
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .col-span-2 { grid-column: span 2; }

  /* ── Card ── */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px;
  }
  .card h2 {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;
    color: var(--muted); margin-bottom: 10px;
  }

  /* ── Stats row ── */
  .stat-row { display: flex; gap: 24px; flex-wrap: wrap; }
  .stat { display: flex; flex-direction: column; }
  .stat .label { font-size: 10px; color: var(--muted); text-transform: uppercase; }
  .stat .value { font-size: 20px; font-weight: bold; margin-top: 2px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neu { color: var(--text); }

  /* ── Markets table ── */
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; font-size: 10px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.08em;
    padding: 4px 6px; border-bottom: 1px solid var(--border);
  }
  td { padding: 5px 6px; border-bottom: 1px solid #161e2e; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #161e2e; }

  /* ── Edge bar ── */
  .edge-bar-wrap { display: flex; align-items: center; gap: 6px; min-width: 90px; }
  .edge-bar-bg {
    flex: 1; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden;
  }
  .edge-bar-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
  .edge-pos { background: var(--green); }
  .edge-neg { background: var(--red); }

  /* ── Trade feed ── */
  .trade-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 5px 0; border-bottom: 1px solid #161e2e; font-size: 12px;
  }
  .trade-item:last-child { border-bottom: none; }
  .trade-ticker { color: var(--blue); font-weight: bold; }
  .trade-action-entry { color: var(--green); }
  .trade-action-exit  { color: var(--red); }

  /* ── Position card ── */
  .pos-item {
    display: flex; justify-content: space-between;
    padding: 5px 0; border-bottom: 1px solid #161e2e;
  }
  .pos-item:last-child { border-bottom: none; }

  /* ── Equity chart ── */
  #equity-chart { width: 100%; height: 80px; }

  /* ── Status dot ── */
  .dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 4px;
  }
  .dot-green { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot-red   { background: var(--red); }
  .dot-yellow { background: var(--yellow); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  .empty { color: var(--muted); font-style: italic; padding: 8px 0; }
  .timestamp { color: var(--muted); font-size: 10px; }
  .conf-bar {
    display: inline-block; width: 36px; height: 4px;
    background: var(--border); border-radius: 2px; vertical-align: middle;
    overflow: hidden;
  }
  .conf-fill { height: 100%; background: var(--purple); border-radius: 2px; }
</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:center;gap:12px;">
    <h1>⬡ KALSHI FOMC BOT</h1>
    <span id="mode-badge" class="badge">—</span>
    <span id="ws-badge" class="badge">WS —</span>
  </div>
  <div class="meta">
    <span>Cycles: <span id="cycle-count">—</span></span>
    <span>Last update: <span id="last-update">—</span></span>
  </div>
</div>

<!-- Stats row -->
<div class="card" style="margin-bottom:12px;">
  <div class="stat-row">
    <div class="stat">
      <span class="label">Balance</span>
      <span class="value neu" id="balance">—</span>
    </div>
    <div class="stat">
      <span class="label">Session P&L</span>
      <span class="value" id="session-pnl">—</span>
    </div>
    <div class="stat">
      <span class="label">Unrealized</span>
      <span class="value" id="unrealized-pnl">—</span>
    </div>
    <div class="stat">
      <span class="label">Open Positions</span>
      <span class="value neu" id="open-positions">—</span>
    </div>
    <div class="stat">
      <span class="label">Signals</span>
      <span class="value" id="signal-count" style="color:var(--blue)">—</span>
    </div>
  </div>
</div>

<!-- Equity mini-chart -->
<div class="card" style="margin-bottom:12px;">
  <h2>Session P&L</h2>
  <canvas id="equity-chart"></canvas>
</div>

<div class="grid" style="margin-bottom:12px;">

  <!-- FOMC Markets -->
  <div class="card col-span-2">
    <h2>FOMC Markets</h2>
    <table>
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Kalshi</th>
          <th>FedWatch</th>
          <th>Edge</th>
          <th>Confidence</th>
          <th>Spread</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody id="markets-tbody">
        <tr><td colspan="7" class="empty">Waiting for data...</td></tr>
      </tbody>
    </table>
  </div>

</div>

<div class="grid">

  <!-- Open Positions -->
  <div class="card">
    <h2>Open Positions</h2>
    <div id="positions-list"><div class="empty">No open positions.</div></div>
  </div>

  <!-- Recent Trades -->
  <div class="card">
    <h2>Recent Trades</h2>
    <div id="trades-list"><div class="empty">No trades yet.</div></div>
  </div>

</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let pnlHistory = [];
let equityCtx  = null;

// ── SSE Connection ─────────────────────────────────────────────────────────
const evtSource = new EventSource("/api/stream");
evtSource.onmessage = (e) => {
  try { render(JSON.parse(e.data)); }
  catch(err) { console.error("Parse error", err); }
};
evtSource.onerror = () => {
  document.getElementById("ws-badge").textContent = "SSE DISCONNECTED";
};

// ── Render ─────────────────────────────────────────────────────────────────
function render(s) {
  // Header
  const modeBadge = document.getElementById("mode-badge");
  modeBadge.textContent = s.mode.toUpperCase();
  modeBadge.className   = "badge " + (s.mode === "live" ? "badge-live" : "badge-paper");

  const wsBadge = document.getElementById("ws-badge");
  wsBadge.textContent = s.ws_connected ? "● WS LIVE" : "○ WS OFF";
  wsBadge.className   = "badge " + (s.ws_connected ? "badge-ws-on" : "badge-ws-off");

  document.getElementById("cycle-count").textContent = s.cycle_count;
  document.getElementById("last-update").textContent  = new Date().toLocaleTimeString();

  // Stats
  document.getElementById("balance").textContent       = "$" + (s.balance_cents / 100).toFixed(2);
  const pnl = s.session_pnl / 100;
  const upnl = s.unrealized_pnl / 100;
  const pnlEl = document.getElementById("session-pnl");
  pnlEl.textContent = (pnl >= 0 ? "+" : "") + "$" + pnl.toFixed(2);
  pnlEl.className   = "value " + (pnl > 0 ? "pos" : pnl < 0 ? "neg" : "neu");
  const upnlEl = document.getElementById("unrealized-pnl");
  upnlEl.textContent = (upnl >= 0 ? "+" : "") + "$" + upnl.toFixed(2);
  upnlEl.className   = "value " + (upnl > 0 ? "pos" : upnl < 0 ? "neg" : "neu");
  document.getElementById("open-positions").textContent = s.open_position_count;
  document.getElementById("signal-count").textContent   = s.signals ? s.signals.length : 0;

  // Equity chart
  pnlHistory.push(pnl);
  if (pnlHistory.length > 200) pnlHistory.shift();
  drawEquityChart();

  // Markets table
  renderMarkets(s.markets);

  // Positions
  renderPositions(s.positions);

  // Trades
  renderTrades(s.recent_trades);
}

function renderMarkets(markets) {
  const tbody = document.getElementById("markets-tbody");
  const entries = Object.values(markets).sort((a, b) => b.edge - a.edge);
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No FOMC markets loaded.</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(m => {
    const edge      = m.edge;
    const edgePct   = Math.min(Math.abs(edge) / 20 * 100, 100);
    const edgeClass = edge > 0 ? "edge-pos" : "edge-neg";
    const edgeColor = edge > 0 ? "#10b981" : "#ef4444";
    const ts        = m.updated_at ? new Date(m.updated_at).toLocaleTimeString() : "—";
    const conf      = Math.round((m.confidence || 0) * 100);
    const shortTick = m.ticker.split("-").slice(-2).join("-");
    return `
      <tr>
        <td style="color:var(--blue);font-weight:bold;" title="${m.ticker}">${shortTick}</td>
        <td>${m.last_price}¢</td>
        <td>${m.fair_value ? m.fair_value + "¢" : "—"}</td>
        <td>
          <div class="edge-bar-wrap">
            <div class="edge-bar-bg">
              <div class="edge-bar-fill ${edgeClass}" style="width:${edgePct}%"></div>
            </div>
            <span style="color:${edgeColor};min-width:30px">${edge > 0 ? "+" : ""}${edge}¢</span>
          </div>
        </td>
        <td>
          <div class="conf-bar"><div class="conf-fill" style="width:${conf}%"></div></div>
          <span style="margin-left:4px;color:var(--muted)">${conf}%</span>
        </td>
        <td style="color:var(--muted)">${m.spread}¢</td>
        <td class="timestamp">${ts}</td>
      </tr>`;
  }).join("");
}

function renderPositions(positions) {
  const el      = document.getElementById("positions-list");
  const entries = Object.values(positions);
  if (!entries.length) {
    el.innerHTML = '<div class="empty">No open positions.</div>';
    return;
  }
  el.innerHTML = entries.map(p => {
    const upnl    = p.unrealized_pnl / 100;
    const upnlStr = (upnl >= 0 ? "+" : "") + "$" + upnl.toFixed(2);
    const color   = upnl > 0 ? "var(--green)" : upnl < 0 ? "var(--red)" : "var(--muted)";
    const short   = p.ticker.split("-").slice(-2).join("-");
    return `
      <div class="pos-item">
        <div>
          <span style="color:var(--blue);font-weight:bold">${short}</span>
          <span style="color:var(--muted);margin-left:8px">${p.side.toUpperCase()} × ${p.contracts}</span>
        </div>
        <div>
          <span style="color:var(--muted);margin-right:8px">${p.entry_cents}¢ entry</span>
          <span style="color:${color};font-weight:bold">${upnlStr}</span>
        </div>
      </div>`;
  }).join("");
}

function renderTrades(trades) {
  const el = document.getElementById("trades-list");
  if (!trades || !trades.length) {
    el.innerHTML = '<div class="empty">No trades yet.</div>';
    return;
  }
  el.innerHTML = [...trades].reverse().slice(0, 15).map(t => {
    const cls   = t.action === "entry" ? "trade-action-entry" : "trade-action-exit";
    const short = t.ticker.split("-").slice(-2).join("-");
    const ts    = new Date(t.timestamp).toLocaleTimeString();
    return `
      <div class="trade-item">
        <div>
          <span class="${cls}">${t.action.toUpperCase()}</span>
          <span class="trade-ticker" style="margin-left:6px">${short}</span>
          <span style="color:var(--muted);margin-left:6px">${t.side} × ${t.contracts} @ ${t.price}¢</span>
        </div>
        <div>
          <span style="color:var(--muted);font-size:11px">${ts}</span>
        </div>
      </div>`;
  }).join("");
}

// ── Equity chart (canvas) ──────────────────────────────────────────────────
function drawEquityChart() {
  const canvas = document.getElementById("equity-chart");
  const ctx    = canvas.getContext("2d");
  const w      = canvas.offsetWidth;
  const h      = canvas.offsetHeight;
  canvas.width  = w;
  canvas.height = h;

  ctx.clearRect(0, 0, w, h);
  if (pnlHistory.length < 2) return;

  const min  = Math.min(...pnlHistory, 0);
  const max  = Math.max(...pnlHistory, 0.01);
  const zero = h - ((0 - min) / (max - min)) * h;

  // Zero line
  ctx.strokeStyle = "#1f2937";
  ctx.lineWidth   = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(0, zero);
  ctx.lineTo(w, zero);
  ctx.stroke();
  ctx.setLineDash([]);

  // P&L line
  const step = w / (pnlHistory.length - 1);
  const last = pnlHistory[pnlHistory.length - 1];
  const lineColor = last >= 0 ? "#10b981" : "#ef4444";

  ctx.strokeStyle = lineColor;
  ctx.lineWidth   = 2;
  ctx.beginPath();
  pnlHistory.forEach((v, i) => {
    const x = i * step;
    const y = h - ((v - min) / (max - min)) * h;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Fill under curve
  ctx.fillStyle = last >= 0 ? "rgba(16,185,129,0.08)" : "rgba(239,68,68,0.08)";
  ctx.lineTo(w, zero);
  ctx.lineTo(0, zero);
  ctx.closePath();
  ctx.fill();
}
</script>
</body>
</html>
"""
