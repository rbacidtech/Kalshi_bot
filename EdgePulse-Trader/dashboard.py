"""
dashboard.py — EdgePulse trading control panel.

Single data source: Redis.  No Flask, no file reads, no HTTP polling.
One pipeline call per refresh cycle.

Layout:
  Sidebar — node status, key numbers, halt/resume, refresh toggle
  Tabs    — Overview | BTC | Kalshi | Positions | History | Controls

Controls tab writes directly to ep:config; the bot reads overrides each cycle.

Run:
  streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import redis
import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

REDIS_URL  = os.getenv("REDIS_URL",             "redis://localhost:6379/0")
REFRESH_S  = int(os.getenv("DASHBOARD_REFRESH_S", "3"))
NODE_STALE = int(os.getenv("NODE_STALE_S",        "120"))   # seconds before node flagged stale

st.set_page_config(
    page_title = "EdgePulse",
    page_icon  = "⚡",
    layout     = "wide",
    initial_sidebar_state = "expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #0d1117; }
[data-testid="stSidebar"]          { background: #0d1117; border-right: 1px solid #21262d; }
.main .block-container             { padding: 1.2rem 2rem 3rem; max-width: 100%; }

#MainMenu, footer, header           { visibility: hidden; }
[data-testid="stToolbar"],
[data-testid="stDecoration"]        { display: none; }

/* Metrics */
[data-testid="metric-container"] {
  background: #161b22;
  border: 1px solid #21262d;
  border-radius: 8px;
  padding: 14px 18px;
}
[data-testid="metric-container"] label {
  font-size: 11px !important; color: #8b949e !important;
  text-transform: uppercase; letter-spacing: .08em;
}
[data-testid="stMetricValue"] {
  font-size: 22px !important; font-weight: 700 !important;
  color: #e6edf3 !important;
  font-family: 'SF Mono','Fira Code',monospace !important;
}

/* DataFrames */
[data-testid="stDataFrame"] { border: 1px solid #21262d; border-radius: 6px; overflow: hidden; }

/* Buttons */
[data-testid="stButton"] > button {
  background: #21262d; border: 1px solid #30363d;
  color: #c9d1d9; border-radius: 6px; font-size: 13px; font-weight: 500;
}
[data-testid="stButton"] > button:hover {
  border-color: #388bfd; color: #58a6ff;
}

/* Tabs */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
  background: transparent; border-bottom: 1px solid #21262d; gap: 0;
}
[data-testid="stTabs"] [data-baseweb="tab"] {
  background: transparent; color: #8b949e;
  font-size: 13px; font-weight: 500;
  border-radius: 0; padding: 8px 18px; border-bottom: 2px solid transparent;
}
[data-testid="stTabs"] [aria-selected="true"] {
  color: #e6edf3 !important; border-bottom: 2px solid #388bfd !important;
  background: transparent !important;
}

/* Dividers */
hr { border-color: #21262d; margin: .8rem 0; }

/* Section labels */
h3 {
  color: #8b949e !important; font-size: 12px !important;
  font-weight: 600 !important; text-transform: uppercase !important;
  letter-spacing: .1em !important; margin: .8rem 0 .4rem !important;
}

/* Node cards */
.node-card {
  background: #161b22; border-radius: 8px;
  padding: 14px 18px; border: 1px solid #21262d;
}
.node-card.ok  { border-color: #3fb95044; }
.node-card.err { border-color: #f8514944; }
.nc-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: .08em; }
.nc-value { font-size: 20px; font-weight: 700; margin: 4px 0 2px; }
.nc-sub   { font-size: 12px; color: #8b949e; }
.ok-text  { color: #3fb950; }
.err-text { color: #f85149; }
.warn-text{ color: #d29922; }
</style>
""", unsafe_allow_html=True)


# ── Redis ─────────────────────────────────────────────────────────────────────

@st.cache_resource
def _redis_conn() -> redis.Redis:
    return redis.from_url(
        REDIS_URL,
        decode_responses       = True,
        socket_connect_timeout = 2,
        socket_keepalive       = True,
    )


def fetch_all(r: redis.Redis) -> Dict[str, Any]:
    """One Redis pipeline round-trip — all data for every tab."""
    try:
        pipe = r.pipeline()
        pipe.hgetall("ep:balance")           # 0
        pipe.hgetall("ep:positions")         # 1
        pipe.hgetall("ep:prices")            # 2
        pipe.hgetall("ep:config")            # 3
        pipe.xlen("ep:signals")              # 4
        pipe.xlen("ep:executions")           # 5
        pipe.xrevrange("ep:executions", count=200)  # 6
        pipe.xrevrange("ep:system",     count=30)   # 7
        pipe.lrange("ep:btc_history",   0, 239)     # 8  rolling BTC snapshots
        raw = pipe.execute()
    except redis.RedisError as exc:
        return {"_error": str(exc)}

    (bal_raw, pos_raw, price_raw, cfg_raw,
     sig_total, exec_total, exec_raw, sys_raw, btc_hist_raw) = raw

    # Balance
    balance_cents = sum(_j(v).get("balance_cents", 0) for v in bal_raw.values())

    # Positions (with unrealized P&L — filled in below)
    positions: Dict[str, dict] = {k: _j(v) for k, v in pos_raw.items() if v}

    # Prices
    prices: Dict[str, dict] = {k: _j(v) for k, v in price_raw.items() if v}

    # Config / LLM policy
    config: Dict[str, str] = cfg_raw or {}

    # Executions
    fills, rejects, expireds = [], [], []
    session_edge = 0.0
    for _, m in exec_raw:
        rep    = _j(m.get("payload", "{}"))
        status = rep.get("status", "")
        if   status == "filled":   fills.append(rep);   session_edge += float(rep.get("edge_captured", 0))
        elif status == "rejected": rejects.append(rep)
        elif status == "expired":  expireds.append(rep)

    # Node heartbeats — most recent ts_us per node from ep:system
    node_ts:   Dict[str, int]  = {}
    events:    List[dict]      = []
    for _, m in sys_raw:
        ev   = _j(m.get("payload", "{}"))
        node = ev.get("node", "")
        ts   = ev.get("ts_us", 0)
        if node and ts > node_ts.get(node, 0):
            node_ts[node] = ts
        events.append(ev)

    # BTC history (newest-first from lpush, so we reverse for charting)
    btc_history = [_j(v) for v in btc_hist_raw if v]

    # Unrealized P&L per position
    open_upnl = 0.0
    for ticker, pos in positions.items():
        pd = prices.get(ticker, {})
        cur  = pd.get("last_price") or pd.get("yes_price")
        ent  = pos.get("entry_cents")
        side = pos.get("side", "yes")
        qty  = pos.get("contracts", 1)
        if cur and ent:
            move = (cur - ent) if side in ("yes", "buy") else (ent - cur)
            pos["_upnl"] = round(move * qty, 2)
            pos["_cur"]  = cur
        else:
            pos["_upnl"] = None
            pos["_cur"]  = None
        if pos["_upnl"] is not None:
            open_upnl += pos["_upnl"]

    return {
        "balance_cents": balance_cents,
        "positions":     positions,
        "prices":        prices,
        "btc":           prices.get("BTC-USD", {}),
        "config":        config,
        "sig_total":     sig_total  or 0,
        "exec_total":    exec_total or 0,
        "fills":         fills,
        "rejects":       rejects,
        "expireds":      expireds,
        "session_edge":  session_edge,
        "node_ts":       node_ts,
        "events":        events,
        "btc_history":   btc_history,
        "open_upnl":     open_upnl,
        "is_halted":     config.get("HALT_TRADING") == "1",
        "fetched_us":    int(time.time() * 1_000_000),
    }


def _j(v) -> dict:
    try:    return json.loads(v) if isinstance(v, str) else {}
    except: return {}


# ── Formatters ────────────────────────────────────────────────────────────────

def usd(cents: Optional[float], dec: int = 2) -> str:
    if cents is None: return "—"
    return f"${cents / 100:,.{dec}f}"

def cents_str(c: Optional[float]) -> str:
    if c is None: return "—"
    return f"{c:+.0f}¢"

def ago(ts_us: Optional[int]) -> str:
    if not ts_us: return "never"
    s = int((time.time() * 1e6 - ts_us) / 1e6)
    if s < 60:   return f"{s}s ago"
    if s < 3600: return f"{s // 60}m ago"
    return f"{s // 3600}h {(s % 3600) // 60}m ago"

def hms(ts_us: Optional[int]) -> str:
    if not ts_us: return "—"
    return datetime.fromtimestamp(ts_us / 1e6, tz=timezone.utc).strftime("%H:%M:%S")

def node_pill(node_ts: dict, fragment: str) -> tuple[str, str, str]:
    """Returns (dot, label, age) for a node matched by id fragment."""
    ts = next((v for k, v in node_ts.items() if fragment in k.lower()), None)
    if not ts:
        return "○", "No heartbeat", "never"
    s  = (time.time() * 1e6 - ts) / 1e6
    ok = s < NODE_STALE
    return ("●" if ok else "○"), ("Online" if ok else "Stale"), ago(ts)


# ── Page bootstrap ────────────────────────────────────────────────────────────

r = _redis_conn()
try:
    r.ping()
    redis_ok = True
except Exception:
    redis_ok = False

d   = fetch_all(r) if redis_ok else {"_error": "Redis unreachable"}
err = d.get("_error")


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### ⚡ EdgePulse")
    st.divider()

    # Node status
    st.markdown("**Nodes**")
    r_dot = "🟢" if redis_ok else "🔴"
    st.markdown(f"{r_dot} Redis")

    if not err:
        for fragment, label in [("intel", "Intel"), ("exec", "Exec")]:
            sym, state_label, age = node_pill(d["node_ts"], fragment)
            dot = "🟢" if sym == "●" else "🔴"
            st.markdown(f"{dot} {label} &nbsp; `{age}`", unsafe_allow_html=True)

    st.divider()

    # Key numbers
    if not err:
        btc_price = d["btc"].get("last_price") or d["btc"].get("yes_price")
        if btc_price:
            st.markdown(f"**BTC** &nbsp; `${btc_price:,.0f}`", unsafe_allow_html=True)
        st.markdown(f"**Balance** &nbsp; `{usd(d['balance_cents'])}`", unsafe_allow_html=True)

        pos_count = len(d["positions"])
        upnl      = d["open_upnl"]
        halted    = d["is_halted"]

        if halted:
            st.error("🛑 Trading halted")
        else:
            st.markdown(
                f"**Positions** `{pos_count}` &nbsp; **uP&L** `{cents_str(upnl)}`",
                unsafe_allow_html=True,
            )

    st.divider()

    # Controls
    auto_refresh = st.toggle("Auto-refresh (3s)", value=True)
    if st.button("↻ Refresh", width="stretch"):
        st.rerun()

    if not err:
        if d["is_halted"]:
            if st.button("▶ Resume", width="stretch", type="primary"):
                r.hset("ep:config", mapping={"HALT_TRADING": "0", "llm_halt_trading": "0"})
                time.sleep(0.2); st.rerun()
        else:
            if st.button("🛑 Halt", width="stretch"):
                r.hset("ep:config", mapping={"HALT_TRADING": "1", "llm_halt_trading": "1"})
                time.sleep(0.2); st.rerun()

    st.divider()
    st.caption(f"Fetched {hms(d.get('fetched_us'))}")


# ── Error state ───────────────────────────────────────────────────────────────

if err:
    st.error(f"**Redis unavailable:** {err}")
    st.code(f"redis-cli -u '{REDIS_URL}' ping", language="bash")
    if auto_refresh:
        time.sleep(REFRESH_S); st.rerun()
    st.stop()


# ── Tabs ──────────────────────────────────────────────────────────────────────

import pandas as pd   # noqa: E402 — after Redis check so errors surface cleanly

t_ov, t_btc, t_kal, t_pos, t_hist, t_ctrl = st.tabs(
    ["Overview", "BTC", "Kalshi", "Positions", "History", "Controls"]
)


# ═══════════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════════

with t_ov:
    # KPIs
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1: st.metric("Balance",        usd(d["balance_cents"], 0))
    with c2: st.metric("Open positions", len(d["positions"]))
    with c3:
        upnl = d["open_upnl"]
        st.metric("Unrealized P&L", cents_str(upnl),
                  delta="▲" if upnl > 0 else ("▼" if upnl < 0 else None),
                  delta_color="normal" if upnl >= 0 else "inverse")
    with c4: st.metric("Session fills",  len(d["fills"]))
    with c5: st.metric("Rejects",        len(d["rejects"]))
    with c6: st.metric("Signal stream",  f"{d['sig_total']:,}")

    st.divider()

    # Node health cards
    st.markdown("### Node health")

    def _node_card(col, label, fragment):
        sym, state_label, age_str = node_pill(d["node_ts"], fragment)
        ok    = sym == "●"
        color = "#3fb950" if ok else "#f85149"
        badge = "ok" if ok else "err"
        with col:
            st.markdown(
                f"<div class='node-card {badge}'>"
                f"<div class='nc-label'>{label}</div>"
                f"<div class='nc-value {'ok-text' if ok else 'err-text'}'>{sym} {state_label}</div>"
                f"<div class='nc-sub'>{age_str}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    nc1, nc2, nc3 = st.columns(3)
    _node_card(nc1, "Intel node (DO NYC3)", "intel")
    _node_card(nc2, "Exec node (QuantVPS)", "exec")

    ok    = redis_ok
    color = "#3fb950" if ok else "#f85149"
    badge = "ok" if ok else "err"
    with nc3:
        st.markdown(
            f"<div class='node-card {badge}'>"
            f"<div class='nc-label'>Redis</div>"
            f"<div class='nc-value {'ok-text' if ok else 'err-text'}'>{'● Connected' if ok else '○ Down'}</div>"
            f"<div class='nc-sub'>signals:{d['sig_total']:,} &nbsp; execs:{d['exec_total']:,}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    # Recent system events
    st.markdown("### System events")
    if d["events"]:
        ev_rows = [
            {
                "Time":   hms(e.get("ts_us")),
                "Node":   e.get("node",       "—"),
                "Event":  e.get("event_type", "—"),
                "Detail": e.get("detail",     ""),
            }
            for e in d["events"][:15]
        ]
        st.dataframe(
            pd.DataFrame(ev_rows),
            width="stretch",
            hide_index=True,
            height=min(38 * len(ev_rows) + 38, 460),
        )
    else:
        st.info("No system events yet — start the Intel or Exec node.")


# ═══════════════════════════════════════════════════════════════════════════════
# BTC
# ═══════════════════════════════════════════════════════════════════════════════

with t_btc:
    btc    = d["btc"]
    price  = btc.get("last_price") or btc.get("yes_price")
    rsi    = btc.get("btc_rsi")
    z      = btc.get("btc_z_score")
    ts_btc = btc.get("ts_us")

    # KPIs
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        st.metric("BTC / USD", f"${price:,.0f}" if price else "—")
    with b2:
        rsi_note = "oversold" if rsi and rsi < 35 else ("overbought" if rsi and rsi > 65 else None)
        st.metric("RSI-14", f"{rsi:.1f}" if rsi is not None else "—",
                  delta=rsi_note,
                  delta_color="inverse" if rsi_note == "overbought" else "normal")
    with b3:
        st.metric("Z-Score", f"{z:.2f}" if z is not None else "—")
    with b4:
        st.metric("Data age", ago(ts_btc))

    st.divider()

    # Price history chart
    hist = d["btc_history"]
    if len(hist) > 5:
        hist_df = pd.DataFrame(reversed(hist))    # oldest → newest
        if "price" in hist_df.columns:
            st.markdown("### Price history (Intel cycles)")
            chart_df = hist_df[["price"]].copy()
            chart_df.index = range(len(chart_df))
            st.line_chart(chart_df, height=220, color="#388bfd")
    else:
        st.info(
            "BTC price history populates as the Intel node runs. "
            "Requires `POLYGON_API_KEY` in `.env`."
        )

    st.divider()

    # BTC fills
    btc_fills = [f for f in d["fills"] if f.get("asset_class") == "btc_spot"]
    st.markdown(f"### BTC fills ({len(btc_fills)})")
    if btc_fills:
        rows = [{
            "Time":  hms(f.get("ts_us")),
            "Side":  f.get("side", "—").upper(),
            "Size":  f.get("contracts", "—"),
            "Fill":  f"${f.get('fill_price', 0):,.2f}",
            "Edge":  f"{f.get('edge_captured', 0):.4f}",
            "Mode":  f.get("mode", "—"),
        } for f in btc_fills[:50]]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.caption(
            "No BTC fills this session. "
            "Signal requires RSI < 35 **and** price < lower BB **and** z < −1.5 simultaneously."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# KALSHI
# ═══════════════════════════════════════════════════════════════════════════════

with t_kal:
    kal_fills   = [f for f in d["fills"]   if f.get("asset_class", "kalshi") == "kalshi"]
    kal_rejects = [f for f in d["rejects"] if f.get("asset_class", "kalshi") == "kalshi"]

    k1, k2, k3 = st.columns(3)
    with k1: st.metric("Kalshi fills",   len(kal_fills))
    with k2: st.metric("Kalshi rejects", len(kal_rejects))
    with k3:
        kal_price_count = sum(1 for k in d["prices"] if k != "BTC-USD")
        st.metric("Live markets",  kal_price_count)

    st.divider()

    st.markdown("### Recent fills")
    if kal_fills:
        rows = [{
            "Time":    hms(f.get("ts_us")),
            "Ticker":  f.get("ticker", "—"),
            "Side":    f.get("side", "—").upper(),
            "Qty":     f.get("contracts", "—"),
            "Fill ¢":  f"{f.get('fill_price', 0) * 100:.0f}¢",
            "Edge":    f"{f.get('edge_captured', 0):.3f}",
            "Mode":    f.get("mode", "—"),
        } for f in kal_fills[:50]]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=260)
    else:
        st.caption("No Kalshi fills yet — signals fire when edge ≥ EDGE_THRESHOLD and confidence ≥ MIN_CONFIDENCE.")

    st.divider()

    # Live Kalshi prices from ep:prices
    kal_prices = {k: v for k, v in d["prices"].items() if k != "BTC-USD"}
    st.markdown(f"### Live market prices ({len(kal_prices)})")
    if kal_prices:
        price_rows = [{
            "Ticker":  t,
            "Yes ¢":   f"{p.get('yes_price', 0):.0f}¢",
            "No ¢":    f"{p.get('no_price', 0):.0f}¢",
            "Spread":  f"{p.get('spread', 0):.0f}¢",
            "Last":    f"{p.get('last_price', 0):.0f}¢",
            "Age":     ago(p.get("ts_us")),
        } for t, p in list(kal_prices.items())[:80]]
        st.dataframe(pd.DataFrame(price_rows), width="stretch", hide_index=True, height=360)
    else:
        st.caption("Price data populates once the Intel node's WebSocket connects.")


# ═══════════════════════════════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════════════════════════════

with t_pos:
    positions = d["positions"]
    open_upnl = d["open_upnl"]

    p1, p2, p3 = st.columns(3)
    with p1: st.metric("Open positions", len(positions))
    with p2: st.metric("Unrealized P&L", cents_str(open_upnl))
    with p3:
        exposure = sum(
            pos.get("entry_cents", 0) * pos.get("contracts", 1)
            for pos in positions.values()
        )
        st.metric("Total exposure", usd(exposure))

    st.divider()

    if positions:
        rows = []
        for ticker, pos in positions.items():
            upnl = pos.get("_upnl")
            rows.append({
                "Ticker":    ticker,
                "Class":     pos.get("asset_class", "kalshi"),
                "Side":      pos.get("side", "—").upper(),
                "Qty":       pos.get("contracts", 1),
                "Entry ¢":   f"{pos.get('entry_cents', 0):.0f}¢",
                "Current ¢": f"{pos.get('_cur', 0):.0f}¢" if pos.get("_cur") else "—",
                "P&L ¢":     f"{upnl:+.0f}¢" if upnl is not None else "—",
                "Opened":    pos.get("entered_at", "")[:16].replace("T", " "),
            })
        df = pd.DataFrame(rows)

        # Colour P&L column — positive green, negative red
        def _colour_pnl(val):
            if val == "—": return ""
            try:
                v = float(val.replace("¢", "").replace("+", ""))
                if v > 0:  return "color:#3fb950;font-weight:600"
                if v < 0:  return "color:#f85149;font-weight:600"
            except Exception:
                pass
            return ""

        styled = df.style.map(_colour_pnl, subset=["P&L ¢"])
        st.dataframe(styled, width="stretch", hide_index=True)

        # Per-position P&L bars
        positions_with_pnl = [(t, p) for t, p in positions.items() if p.get("_upnl") is not None]
        if positions_with_pnl:
            st.divider()
            st.markdown("### P&L breakdown")
            for ticker, pos in positions_with_pnl:
                upnl  = pos["_upnl"]
                color = "#3fb950" if upnl >= 0 else "#f85149"
                cl, cm, cr = st.columns([2, 6, 1])
                with cl: st.caption(ticker[:28])
                with cm:
                    # Normalise ±50¢ → 0–1 progress
                    norm = min(max((upnl + 50) / 100, 0.0), 1.0)
                    st.progress(norm)
                with cr:
                    st.markdown(
                        f"<span style='color:{color};font-weight:600'>{upnl:+.0f}¢</span>",
                        unsafe_allow_html=True,
                    )
    else:
        st.info("No open positions. They appear here when the Exec node places orders.")


# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

with t_hist:
    fills   = d["fills"]
    rejects = d["rejects"]
    expired = d["expireds"]
    edge    = d["session_edge"]

    h1, h2, h3, h4 = st.columns(4)
    with h1: st.metric("Fills",        len(fills))
    with h2: st.metric("Rejects",      len(rejects))
    with h3: st.metric("Expired",      len(expired))
    with h4: st.metric("Session edge", f"{edge:.4f}")

    st.divider()

    if fills:
        # Running cumulative edge chart
        running, series = 0.0, []
        for f in reversed(fills):
            running += float(f.get("edge_captured", 0))
            series.append(running)
        if len(series) > 1:
            st.markdown("### Cumulative session edge")
            st.line_chart(
                pd.DataFrame({"edge": series}),
                height=180,
                color=["#3fb950" if series[-1] >= 0 else "#f85149"],
            )
            st.divider()

        st.markdown("### Fill log")
        fill_rows = [{
            "Time":   hms(f.get("ts_us")),
            "Ticker": f.get("ticker",      "—"),
            "Class":  f.get("asset_class", "—"),
            "Side":   f.get("side",        "—").upper(),
            "Qty":    f.get("contracts",   "—"),
            "Fill":   (
                f"{f.get('fill_price', 0) * 100:.0f}¢"
                if f.get("asset_class") == "kalshi"
                else f"${f.get('fill_price', 0):,.2f}"
            ),
            "Edge":   f"{f.get('edge_captured', 0):.4f}",
            "Mode":   f.get("mode", "paper"),
        } for f in fills]
        st.dataframe(
            pd.DataFrame(fill_rows),
            width="stretch",
            hide_index=True,
            height=400,
        )
    else:
        st.info("No fills in the execution stream. Fills appear as the Exec node processes signals.")

    if rejects:
        with st.expander(f"Rejected signals ({len(rejects)})"):
            rej_rows = [{
                "Time":   hms(rej.get("ts_us")),
                "Ticker": rej.get("ticker",        "—"),
                "Reason": rej.get("reject_reason", "—"),
            } for rej in rejects]
            st.dataframe(pd.DataFrame(rej_rows), width="stretch", hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# CONTROLS
# ═══════════════════════════════════════════════════════════════════════════════

with t_ctrl:
    config    = d["config"]
    is_halted = d["is_halted"]

    # ── Emergency ─────────────────────────────────────────────────────────────
    st.markdown("### Emergency")
    ec1, ec2 = st.columns([1, 3])
    with ec1:
        if is_halted:
            if st.button("▶ Resume trading", type="primary", width="stretch"):
                r.hset("ep:config", mapping={"HALT_TRADING": "0", "llm_halt_trading": "0"})
                time.sleep(0.2); st.rerun()
        else:
            if st.button("🛑 Halt all trading", width="stretch"):
                r.hset("ep:config", mapping={"HALT_TRADING": "1", "llm_halt_trading": "1"})
                time.sleep(0.2); st.rerun()
    with ec2:
        if is_halted:
            st.error("Trading is **halted** — new signals are dropped. Click Resume to restart.")
        else:
            st.success("Trading is **active** — signals are flowing normally.")

    st.divider()

    # ── Position sizing ───────────────────────────────────────────────────────
    st.markdown("### Position sizing")
    ps1, ps2 = st.columns(2)
    with ps1:
        cur = float(config.get("llm_scale_factor", "1.0"))
        val = st.slider("Scale factor", 0.1, 2.0, cur, step=0.05,
                         help="Multiplies Kelly-sized positions. 1.0 = full Kelly fraction.")
        if st.button("Apply scale", key="k_scale"):
            r.hset("ep:config", "llm_scale_factor", str(val))
            st.success(f"Scale → {val:.2f}×")

    with ps2:
        cur = min(float(config.get("llm_kelly_fraction", "0.25")), 0.40)
        val = st.slider("Kelly fraction", 0.05, 0.40, cur, step=0.01,
                         help="Fraction of Kelly criterion to trade. 0.25 = quarter-Kelly.")
        if st.button("Apply Kelly", key="k_kelly"):
            r.hset("ep:config", "llm_kelly_fraction", str(val))
            st.success(f"Kelly → {val:.2f}")

    st.divider()

    # ── Strategy toggles ──────────────────────────────────────────────────────
    st.markdown("### Strategy")
    sg1, sg2 = st.columns(2)
    with sg1:
        btc_on = config.get("llm_btc_enabled", "1") != "0"
        if st.toggle("BTC mean-reversion", value=btc_on,
                      help="Pauses BTC signal generation without stopping the bot.") != btc_on:
            r.hset("ep:config", "llm_btc_enabled", "1" if not btc_on else "0")
            st.rerun()
    with sg2:
        kal_on = config.get("llm_kalshi_enabled", "1") != "0"
        if st.toggle("Kalshi FOMC", value=kal_on,
                      help="Pauses Kalshi signal publishing without stopping the bot.") != kal_on:
            r.hset("ep:config", "llm_kalshi_enabled", "1" if not kal_on else "0")
            st.rerun()

    st.divider()

    # ── BTC signal thresholds ─────────────────────────────────────────────────
    st.markdown("### BTC thresholds")
    th1, th2, th3 = st.columns(3)
    with th1:
        cur = int(float(config.get("llm_rsi_oversold", "35")))
        val = st.slider("RSI oversold", 20, 45, cur, step=1,
                         help="RSI below this = oversold = potential LONG signal.")
        if st.button("Apply", key="k_os"):
            r.hset("ep:config", "llm_rsi_oversold", str(val))
            st.success(f"RSI oversold → {val}")
    with th2:
        cur = int(float(config.get("llm_rsi_overbought", "65")))
        val = st.slider("RSI overbought", 55, 80, cur, step=1,
                         help="RSI above this = overbought = potential SHORT signal.")
        if st.button("Apply", key="k_ob"):
            r.hset("ep:config", "llm_rsi_overbought", str(val))
            st.success(f"RSI overbought → {val}")
    with th3:
        cur = float(config.get("llm_z_threshold", "1.5"))
        val = st.slider("Z-score threshold", 0.5, 3.0, cur, step=0.1,
                         help="Minimum |z-score| required to generate a signal.")
        if st.button("Apply", key="k_z"):
            r.hset("ep:config", "llm_z_threshold", str(val))
            st.success(f"Z threshold → {val:.1f}")

    st.divider()

    # ── LLM policy ────────────────────────────────────────────────────────────
    st.markdown("### Claude LLM policy")

    last_run = config.get("llm_last_run_ts")
    run_str  = ago(int(last_run) * 1_000_000) if last_run else "never"
    notes    = config.get("llm_notes", "—")

    st.caption(f"Last run: **{run_str}** · Notes: *{notes}*")

    llm_keys = sorted(k for k in config if k.startswith("llm_") and k not in ("llm_notes", "llm_last_run_ts"))
    if llm_keys:
        st.dataframe(
            pd.DataFrame([{"Parameter": k[4:], "Value": config[k]} for k in llm_keys]),
            width="stretch",
            hide_index=True,
            height=min(38 * len(llm_keys) + 38, 280),
        )

    lc1, lc2 = st.columns([1, 1])
    with lc1:
        if st.button("▶ Run LLM now", width="stretch",
                      help="Launch llm_agent.py one-shot. Policy updates in ~10s."):
            try:
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).parent / "llm_agent.py")],
                    cwd    = str(Path(__file__).parent),
                    stdout = subprocess.DEVNULL,
                    stderr = subprocess.DEVNULL,
                )
                st.info("LLM agent started. Policy will update in ~10s.")
            except Exception as exc:
                st.error(f"Failed: {exc}")
    with lc2:
        if st.button("✕ Clear LLM overrides", width="stretch",
                      help="Delete all llm_* keys from ep:config, reverting to .env defaults."):
            keys_to_del = [k for k in r.hkeys("ep:config") if k.startswith("llm_")]
            if keys_to_del:
                r.hdel("ep:config", *keys_to_del)
            st.success(f"Cleared {len(keys_to_del)} override(s).")
            time.sleep(0.2); st.rerun()

    with st.expander("Raw ep:config"):
        st.json(config)


# ── Auto-refresh ──────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(REFRESH_S)
    st.rerun()
