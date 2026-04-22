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
    page_icon  = "🚀",
    layout     = "wide",
    initial_sidebar_state = "expanded",
)

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Reset / Base ─────────────────────────────────────────────────────── */
[data-testid="stAppViewContainer"] { background: #0d1117; }
[data-testid="stSidebar"] {
  background: #0d1117;
  border-right: 1px solid #21262d;
}
.main .block-container { padding: 0 2rem 3rem; max-width: 100%; }
#MainMenu, footer, header { visibility: hidden; }
[data-testid="stToolbar"],
[data-testid="stDecoration"] { display: none; }

/* ── Gradient top border ──────────────────────────────────────────────── */
[data-testid="stAppViewContainer"]::before {
  content: "";
  display: block;
  height: 3px;
  background: linear-gradient(90deg, #388bfd 0%, #3fb950 35%, #79c0ff 65%, #388bfd 100%);
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 9999;
}

/* ── Custom scrollbar ─────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #0d1117; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }

/* ── Metrics ──────────────────────────────────────────────────────────── */
[data-testid="metric-container"] {
  background: #161b22;
  border: 1px solid #21262d;
  border-radius: 8px;
  padding: 14px 18px;
  position: relative;
  overflow: hidden;
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

/* ── DataFrames ───────────────────────────────────────────────────────── */
[data-testid="stDataFrame"] {
  border: 1px solid #21262d; border-radius: 6px; overflow: hidden;
}

/* ── Buttons ──────────────────────────────────────────────────────────── */
[data-testid="stButton"] > button {
  background: #21262d; border: 1px solid #30363d;
  color: #c9d1d9; border-radius: 6px; font-size: 13px; font-weight: 500;
  transition: border-color .15s, color .15s;
}
[data-testid="stButton"] > button:hover {
  border-color: #388bfd; color: #58a6ff;
}

/* ── Tabs ─────────────────────────────────────────────────────────────── */
[data-testid="stTabs"] [data-baseweb="tab-list"] {
  background: transparent; border-bottom: 1px solid #21262d; gap: 0;
}
[data-testid="stTabs"] [data-baseweb="tab"] {
  background: transparent; color: #8b949e;
  font-size: 13px; font-weight: 500;
  border-radius: 0; padding: 10px 20px;
  border-bottom: 2px solid transparent;
  transition: color .15s;
}
[data-testid="stTabs"] [aria-selected="true"] {
  color: #e6edf3 !important;
  border-bottom: 2px solid #388bfd !important;
  background: transparent !important;
}

/* ── Dividers ─────────────────────────────────────────────────────────── */
hr { border-color: #21262d; margin: .8rem 0; }

/* ── Section labels ───────────────────────────────────────────────────── */
h3 {
  color: #8b949e !important; font-size: 12px !important;
  font-weight: 600 !important; text-transform: uppercase !important;
  letter-spacing: .1em !important; margin: .8rem 0 .4rem !important;
}

/* ── Node cards ───────────────────────────────────────────────────────── */
.node-card {
  background: #161b22;
  border-radius: 8px;
  padding: 0 18px 14px;
  border: 1px solid #21262d;
  position: relative;
  overflow: hidden;
  transition: box-shadow .2s;
}
.node-card::before {
  content: "";
  display: block;
  height: 3px;
  border-radius: 8px 8px 0 0;
  margin: 0 -18px 14px;
}
.node-card.ok  { border-color: #3fb95033; }
.node-card.ok::before { background: #3fb950; box-shadow: 0 0 12px #3fb95066; }
.node-card.ok  { box-shadow: 0 0 16px #3fb95014; }
.node-card.err { border-color: #f8514933; }
.node-card.err::before { background: #f85149; box-shadow: 0 0 12px #f8514966; }
.node-card.err { box-shadow: 0 0 16px #f8514914; }
.nc-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: .08em; }
.nc-value { font-size: 20px; font-weight: 700; margin: 4px 0 2px; display: flex; align-items: center; gap: 8px; }
.nc-sub   { font-size: 12px; color: #8b949e; }
.ok-text  { color: #3fb950; }
.err-text { color: #f85149; }
.warn-text{ color: #e3b341; }

/* ── Pulse animation (online dots) ───────────────────────────────────── */
@keyframes pulse-green {
  0%, 100% { opacity: 1; text-shadow: 0 0 6px #3fb950; }
  50%       { opacity: .6; text-shadow: 0 0 2px #3fb950; }
}
@keyframes pulse-red {
  0%, 100% { opacity: 1; }
  50%       { opacity: .4; }
}
.dot-online  { animation: pulse-green 2s ease-in-out infinite; color: #3fb950; }
.dot-offline { animation: pulse-red   1.5s ease-in-out infinite; color: #f85149; }

/* ── Badge pills ──────────────────────────────────────────────────────── */
.badge {
  display: inline-block;
  padding: 2px 8px; border-radius: 12px;
  font-size: 11px; font-weight: 600; line-height: 1.6;
}
.badge-green  { background: #3fb95022; color: #3fb950; border: 1px solid #3fb95044; }
.badge-red    { background: #f8514922; color: #f85149; border: 1px solid #f8514944; }
.badge-blue   { background: #388bfd22; color: #58a6ff; border: 1px solid #388bfd44; }
.badge-orange { background: #e3b34122; color: #e3b341; border: 1px solid #e3b34144; }
.badge-purple { background: #bc8cff22; color: #bc8cff; border: 1px solid #bc8cff44; }
.badge-cyan   { background: #79c0ff22; color: #79c0ff; border: 1px solid #79c0ff44; }

/* ── Metric cards with left border stripe ─────────────────────────────── */
.kpi-card {
  background: #161b22;
  border: 1px solid #21262d;
  border-left: 3px solid;
  border-radius: 0 8px 8px 0;
  padding: 14px 18px;
  margin-bottom: 8px;
}
.kpi-blue   { border-left-color: #388bfd; }
.kpi-green  { border-left-color: #3fb950; }
.kpi-red    { border-left-color: #f85149; }
.kpi-orange { border-left-color: #e3b341; }
.kpi-purple { border-left-color: #bc8cff; }
.kpi-cyan   { border-left-color: #79c0ff; }
.kpi-label  { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: .08em; }
.kpi-value  { font-size: 22px; font-weight: 700; color: #e6edf3;
              font-family: 'SF Mono','Fira Code',monospace; margin-top: 4px; }
.kpi-sub    { font-size: 12px; color: #8b949e; margin-top: 2px; }

/* ── LLM notes card ───────────────────────────────────────────────────── */
.llm-card {
  background: #161b22;
  border: 1px solid #21262d;
  border-left: 3px solid #bc8cff;
  border-radius: 0 8px 8px 0;
  padding: 14px 18px;
  margin: 12px 0;
}
.llm-card .llm-label { font-size: 11px; color: #bc8cff; text-transform: uppercase;
                        letter-spacing: .08em; margin-bottom: 6px; }
.llm-card .llm-notes { font-size: 13px; color: #c9d1d9; line-height: 1.5; }
.llm-card .llm-meta  { font-size: 11px; color: #484f58; margin-top: 8px; }

/* ── Settings cards ───────────────────────────────────────────────────── */
.settings-card {
  background: #161b22;
  border: 1px solid #21262d;
  border-radius: 8px;
  padding: 16px 18px;
  margin-bottom: 12px;
}
.settings-card-header {
  font-size: 12px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .1em; color: #8b949e; margin-bottom: 12px;
  padding-bottom: 8px; border-bottom: 1px solid #21262d;
}

/* ── Sidebar brand ────────────────────────────────────────────────────── */
.brand-header {
  font-size: 22px; font-weight: 700; color: #e6edf3;
  letter-spacing: -.02em; padding: 8px 0 4px;
  display: flex; align-items: center; gap: 8px;
}
.brand-header span { color: #58a6ff; }

/* ── Rocket animation ─────────────────────────────────────────────────── */
@keyframes rocket-launch {
  0%   { transform: translateY(0px)   rotate(-45deg); }
  25%  { transform: translateY(-5px)  rotate(-45deg); }
  50%  { transform: translateY(-9px)  rotate(-45deg); }
  75%  { transform: translateY(-5px)  rotate(-45deg); }
  100% { transform: translateY(0px)   rotate(-45deg); }
}
@keyframes exhaust-flicker {
  0%, 100% { opacity: 1;   transform: scaleY(1); }
  33%       { opacity: 0.6; transform: scaleY(0.7); }
  66%       { opacity: 0.9; transform: scaleY(1.2); }
}
.rocket-wrap {
  position: relative; display: inline-block;
  width: 32px; height: 32px; flex-shrink: 0;
}
.rocket-body {
  font-size: 24px; line-height: 1;
  display: inline-block;
  animation: rocket-launch 2.4s ease-in-out infinite;
  filter: drop-shadow(0 0 6px #388bfd88);
}
.rocket-exhaust {
  position: absolute; bottom: -2px; left: 50%;
  transform: translateX(-50%) rotate(-45deg) translateY(4px);
  font-size: 11px; line-height: 1;
  animation: exhaust-flicker 0.35s ease-in-out infinite;
  transform-origin: top center;
}

/* ── Sliders ──────────────────────────────────────────────────────────── */
[data-testid="stSlider"] > div > div > div > div {
  background: #388bfd !important;
}

/* ── Number inputs ─────────────────────────────────────────────────────── */
[data-testid="stNumberInput"] input {
  background: #0d1117; border-color: #30363d; color: #e6edf3;
  border-radius: 6px;
}

/* ── Caption / small text ──────────────────────────────────────────────── */
[data-testid="stCaptionContainer"] p {
  color: #484f58 !important; font-size: 11px !important;
}
</style>
""", unsafe_allow_html=True)


# ── Redis ──────────────────────────────────────────────────────────────────────

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
        pipe.xrevrange("ep:executions", count=5000)  # 6  stream cap is 5000; read all
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
    # entry fills: cost_cents > 0, edge_captured = signal edge (dimensionless 0-1 fraction)
    # exit  fills: cost_cents == 0, edge_captured = pnl_cents / 100 (dollar P&L)
    fills, rejects, expireds = [], [], []
    session_edge       = 0.0   # sum of entry signal edges (quality metric, dimensionless)
    realized_pnl_cents = 0.0   # sum of exit P&L in cents (actual dollar outcome)
    for _, m in exec_raw:
        rep    = _j(m.get("payload", "{}"))
        status = rep.get("status", "")
        if status == "filled":
            fills.append(rep)
            if rep.get("cost_cents", 0) > 0:
                # entry fill — edge_captured is the signal's dimensionless edge fraction
                session_edge += float(rep.get("edge_captured", 0))
            else:
                # exit fill — edge_captured = pnl_cents / 100 (convert back to cents)
                realized_pnl_cents += float(rep.get("edge_captured", 0)) * 100
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
    # BTC prices in ep:prices are raw USD (e.g. 75111.53); entry_cents was computed as
    # int(price_usd * BTC_UNIT * 100) where BTC_UNIT=0.0001 (1 contract = 0.0001 BTC).
    # Kalshi prices are already integer cents (0-100).  Normalise before subtraction.
    _BTC_UNIT = 0.0001
    open_upnl = 0.0
    for ticker, pos in positions.items():
        pd   = prices.get(ticker, {})
        cur  = pd.get("last_price") or pd.get("yes_price")
        ent  = pos.get("entry_cents")
        side = pos.get("side", "yes")
        qty  = pos.get("contracts", 1)
        if cur and ent:
            # Normalise BTC current price to the same cent-scale as entry_cents
            if pos.get("asset_class") == "btc":
                cur_cents = int(float(cur) * _BTC_UNIT * 100)
            else:
                cur_cents = int(cur)
            if side in ("yes", "buy"):
                # YES / BTC-long: current price vs entry price
                move = cur_cents - ent
            else:
                # NO position: entry_cents is what we paid for NO (e.g. 80¢).
                # cur_cents is the current YES price; current NO = 100 - cur_cents.
                # P&L per contract = current_NO - entry_NO = (100 - cur_cents) - ent
                move = (100 - cur_cents) - ent
            pos["_upnl"] = round(move * qty, 2)
            pos["_cur"]  = cur_cents
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
        "session_edge":      session_edge,
        "realized_pnl_cents": realized_pnl_cents,
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


# ── Formatters ─────────────────────────────────────────────────────────────────

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

def node_pill(node_ts: dict, fragment: str) -> tuple:
    """Returns (dot, label, age) for a node matched by id fragment."""
    ts = next((v for k, v in node_ts.items() if fragment in k.lower()), None)
    if not ts:
        return ("○", "No heartbeat", "never")
    s  = (time.time() * 1e6 - ts) / 1e6
    ok = s < NODE_STALE
    return ("●" if ok else "○"), ("Online" if ok else "Stale"), ago(ts)


# ── Page bootstrap ─────────────────────────────────────────────────────────────

r = _redis_conn()
try:
    r.ping()
    redis_ok = True
except Exception:
    redis_ok = False

d   = fetch_all(r) if redis_ok else {"_error": "Redis unreachable"}
err = d.get("_error")


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    # Brand header
    st.markdown(
        "<div class='brand-header'>"
        "<div class='rocket-wrap'>"
        "<span class='rocket-body'>🚀</span>"
        "<span class='rocket-exhaust'>🔥</span>"
        "</div>"
        "Edge<span>Pulse</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.divider()

    # Node status
    st.markdown("**Nodes**")

    # Redis node
    r_class = "dot-online" if redis_ok else "dot-offline"
    r_dot   = "●" if redis_ok else "○"
    r_label = "Connected" if redis_ok else "Down"
    st.markdown(
        f"<span class='{r_class}'>{r_dot}</span> Redis &nbsp; "
        f"<span style='color:#484f58;font-size:11px'>{r_label}</span>",
        unsafe_allow_html=True,
    )

    if not err:
        for fragment, label in [("intel", "Intel"), ("exec", "Exec")]:
            sym, state_label, age = node_pill(d["node_ts"], fragment)
            dot_class = "dot-online" if sym == "●" else "dot-offline"
            st.markdown(
                f"<span class='{dot_class}'>{sym}</span> {label} &nbsp; "
                f"<code style='font-size:10px;color:#484f58'>{age}</code>",
                unsafe_allow_html=True,
            )

    st.divider()

    # Key numbers
    if not err:
        btc_price = d["btc"].get("last_price") or d["btc"].get("yes_price")
        if btc_price:
            st.markdown(
                f"**BTC** &nbsp; <code>${btc_price:,.0f}</code>",
                unsafe_allow_html=True,
            )
        st.markdown(
            f"**Balance** &nbsp; <code>{usd(d['balance_cents'])}</code>",
            unsafe_allow_html=True,
        )

        pos_count = len(d["positions"])
        upnl      = d["open_upnl"]
        halted    = d["is_halted"]
        se        = d["session_edge"]
        rpnl      = d["realized_pnl_cents"]

        if halted:
            st.markdown(
                "<span class='badge badge-red'>HALTED</span>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<span class='badge badge-green'>ACTIVE</span>",
                unsafe_allow_html=True,
            )

        st.markdown(
            f"**Positions** <code>{pos_count}</code> &nbsp; "
            f"**uP&L** <code style='color:{'#3fb950' if upnl >= 0 else '#f85149'}'>"
            f"{cents_str(upnl)}</code>",
            unsafe_allow_html=True,
        )
        edge_color = "#3fb950" if se >= 0 else "#f85149"
        rpnl_color = "#3fb950" if rpnl >= 0 else "#f85149"
        st.markdown(
            f"**Session edge** <code style='color:{edge_color}'>{se:.4f}</code> &nbsp; "
            f"**rP&L** <code style='color:{rpnl_color}'>{cents_str(rpnl)}</code>",
            unsafe_allow_html=True,
        )

        # LLM last run age
        last_run = d["config"].get("llm_last_run_ts")
        run_age  = ago(int(last_run) * 1_000_000) if last_run else "never"
        st.markdown(
            f"**LLM** <code style='font-size:10px;color:#484f58'>{run_age}</code>",
            unsafe_allow_html=True,
        )

    st.divider()

    # Controls
    auto_refresh = st.toggle("Auto-refresh (3s)", value=True)
    if st.button("↻ Refresh", use_container_width=True):
        st.rerun()

    if not err:
        if d["is_halted"]:
            if st.button("▶ Resume", use_container_width=True, type="primary"):
                r.hset("ep:config", mapping={"HALT_TRADING": "0", "llm_halt_trading": "0"})
                time.sleep(0.2); st.rerun()
        else:
            if st.button("🛑 Halt", use_container_width=True):
                r.hset("ep:config", mapping={"HALT_TRADING": "1", "llm_halt_trading": "1"})
                time.sleep(0.2); st.rerun()

    st.divider()
    st.caption(f"Fetched {hms(d.get('fetched_us'))}")


# ── Error state ────────────────────────────────────────────────────────────────

if err:
    st.error(f"**Redis unavailable:** {err}")
    st.code(f"redis-cli -u '{REDIS_URL}' ping", language="bash")
    if auto_refresh:
        time.sleep(REFRESH_S); st.rerun()
    st.stop()


# ── Stale-node alerts (shown above tabs on every page) ────────────────────────
# Only fire when at least one heartbeat has been seen — suppresses false alarms
# during the first 2 minutes after a fresh restart before any heartbeat arrives.
if not err and d.get("node_ts"):
    _now_us = time.time() * 1e6
    for _frag, _label in [("intel", "Intel (DO NYC3)"), ("exec", "QuantVPS Exec")]:
        _ts = next((v for k, v in d["node_ts"].items() if _frag in k.lower()), None)
        if _ts and (_now_us - _ts) / 1e6 > NODE_STALE:
            st.warning(
                f"⚠️ **{_label}** node is stale — last heartbeat {ago(_ts)} "
                f"(threshold {NODE_STALE}s). Check: `systemctl status edgepulse`"
            )


# ── Tabs ───────────────────────────────────────────────────────────────────────

import pandas as pd   # noqa: E402 — after Redis check so errors surface cleanly

t_ov, t_btc, t_kal, t_pos, t_hist, t_ctrl = st.tabs(
    ["Overview", "BTC", "Kalshi", "Positions", "History", "Controls"]
)


# ═══════════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════════

with t_ov:
    # KPI row with colored left-border stripe cards
    c1, c2, c3, c4, c5, c6 = st.columns(6)

    def _kpi(col, label, value, sub="", color="blue"):
        with col:
            st.markdown(
                f"<div class='kpi-card kpi-{color}'>"
                f"<div class='kpi-label'>{label}</div>"
                f"<div class='kpi-value'>{value}</div>"
                f"{'<div class=kpi-sub>' + sub + '</div>' if sub else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )

    upnl = d["open_upnl"]
    upnl_color = "green" if upnl >= 0 else "red"

    _kpi(c1, "Balance",        usd(d["balance_cents"], 0),  color="blue")
    _kpi(c2, "Open positions", str(len(d["positions"])),     color="cyan")
    _kpi(c3, "Unrealized P&L", cents_str(upnl),              color=upnl_color)
    _kpi(c4, "Session fills",  str(len(d["fills"])),         color="green")
    _kpi(c5, "Rejects",        str(len(d["rejects"])),       color="orange")
    _kpi(c6, "Signal stream",  f"{d['sig_total']:,}",        color="purple")

    st.divider()

    # Node health cards
    st.markdown("### Node health")

    def _node_card(col, label, fragment):
        sym, state_label, age_str = node_pill(d["node_ts"], fragment)
        ok      = sym == "●"
        badge   = "ok" if ok else "err"
        dot_cls = "dot-online" if ok else "dot-offline"
        txt_cls = "ok-text" if ok else "err-text"
        with col:
            st.markdown(
                f"<div class='node-card {badge}'>"
                f"<div class='nc-label'>{label}</div>"
                f"<div class='nc-value {txt_cls}'>"
                f"<span class='{dot_cls}'>{sym}</span> {state_label}"
                f"</div>"
                f"<div class='nc-sub'>{age_str}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    nc1, nc2, nc3 = st.columns(3)
    _node_card(nc1, "Intel node (DO NYC3)", "intel")
    _node_card(nc2, "Exec node (QuantVPS)", "exec")

    ok      = redis_ok
    badge   = "ok" if ok else "err"
    dot_cls = "dot-online" if ok else "dot-offline"
    txt_cls = "ok-text" if ok else "err-text"
    dot_sym = "●" if ok else "○"
    with nc3:
        st.markdown(
            f"<div class='node-card {badge}'>"
            f"<div class='nc-label'>Redis</div>"
            f"<div class='nc-value {txt_cls}'>"
            f"<span class='{dot_cls}'>{dot_sym}</span> {'Connected' if ok else 'Down'}"
            f"</div>"
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
            use_container_width=True,
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

            # RSI and Z overlay if available
            if "rsi" in hist_df.columns and hist_df["rsi"].notna().any():
                rc1, rc2 = st.columns(2)
                with rc1:
                    st.markdown("### RSI-14 history")
                    rsi_df = hist_df[["rsi"]].copy()
                    rsi_df.index = range(len(rsi_df))
                    st.line_chart(rsi_df, height=160, color="#e3b341")
                with rc2:
                    st.markdown("### Z-score history")
                    z_df = hist_df[["z"]].copy() if "z" in hist_df.columns else None
                    if z_df is not None:
                        z_df.index = range(len(z_df))
                        st.line_chart(z_df, height=160, color="#bc8cff")
    else:
        st.info(
            "BTC price history populates after the first Intel poll cycle (~2 min). "
            "Coinbase Exchange candles are used automatically — no API key needed."
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
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption(
            "No BTC fills yet. Signal fires when RSI < 35 (oversold) or RSI > 65 (overbought), "
            "price outside Bollinger Band, and |z-score| > 1.5 simultaneously. "
            "Thresholds are adjustable in the Controls tab."
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
        st.metric("Live markets", kal_price_count)

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
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=260)
    else:
        st.caption("No Kalshi fills yet — signals fire when edge >= EDGE_THRESHOLD and confidence >= MIN_CONFIDENCE.")

    st.divider()

    kal_prices = {k: v for k, v in d["prices"].items() if k != "BTC-USD"}
    st.markdown(f"### Live market prices ({len(kal_prices)})")
    if kal_prices:
        # Warn if the freshest price is older than 5 minutes
        _freshest_ts = max((p.get("ts_us") or 0 for p in kal_prices.values()), default=0)
        _price_age_s = (time.time() * 1e6 - _freshest_ts) / 1e6 if _freshest_ts else None
        if _price_age_s is not None and _price_age_s > 300:
            st.warning(
                f"⚠️ Kalshi prices are stale — most recent update {ago(_freshest_ts)}. "
                "Intel node may be paused or disconnected."
            )
        _stale_cutoff_us = time.time() * 1e6 - 300 * 1e6   # 5 min
        price_rows = [{
            "Ticker":  t,
            "Yes ¢":   f"{p.get('yes_price', 0):.0f}¢",
            "No ¢":    f"{p.get('no_price', 0):.0f}¢",
            "Spread":  f"{p.get('spread', 0):.0f}¢",
            "Last":    f"{p.get('last_price', 0):.0f}¢",
            "Age":     ago(p.get("ts_us")),
            "Fresh":   "✓" if (p.get("ts_us") or 0) >= _stale_cutoff_us else "⚠",
        } for t, p in list(kal_prices.items())[:80]]
        st.dataframe(pd.DataFrame(price_rows), use_container_width=True, hide_index=True, height=360)
    else:
        st.caption("Price data populates once the Intel node's WebSocket connects.")


# ═══════════════════════════════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 datetime string → UTC datetime, returns None on failure."""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).astimezone(timezone.utc)
    except Exception:
        return None

def _days_until(close_str: Optional[str]) -> Optional[float]:
    """Float days from now until close_time. Negative = already past."""
    dt = _parse_dt(close_str)
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400

def _days_held(entered_str: Optional[str]) -> Optional[float]:
    """Float days since entered_at."""
    dt = _parse_dt(entered_str)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400

def _fmt_close(close_str: Optional[str]) -> str:
    """'Dec 9, 2026  18:55 UTC'"""
    dt = _parse_dt(close_str)
    if dt is None:
        return "—"
    return dt.strftime("%-d %b %Y  %H:%M UTC")

def _days_badge(days: Optional[float]) -> str:
    if days is None:
        return "—"
    if days < 0:
        return f"<span class='badge badge-red'>PAST  {abs(days):.0f}d ago</span>"
    if days <= 7:
        return f"<span class='badge badge-red'>{days:.0f}d</span>"
    if days <= 30:
        return f"<span class='badge badge-orange'>{days:.0f}d</span>"
    if days <= 90:
        return f"<span class='badge badge-blue'>{days:.0f}d</span>"
    return f"<span class='badge badge-cyan'>{days:.0f}d</span>"

def _held_str(days: Optional[float]) -> str:
    if days is None:
        return "—"
    if days < 1:
        return f"{int(days * 24)}h"
    return f"{days:.0f}d"

with t_pos:
    positions = d["positions"]
    open_upnl = d["open_upnl"]

    # ── KPIs ──────────────────────────────────────────────────────────────────
    # Find next upcoming close among Kalshi positions
    kal_closes = [
        _days_until(p.get("close_time"))
        for p in positions.values()
        if p.get("close_time") and p.get("asset_class", "kalshi") == "kalshi"
    ]
    next_close_days = min((d for d in kal_closes if d is not None and d > 0), default=None)

    exposure = sum(
        pos.get("entry_cents", 0) * pos.get("contracts", 1)
        for pos in positions.values()
    )

    p1, p2, p3, p4 = st.columns(4)
    with p1: st.metric("Open positions",  len(positions))
    with p2: st.metric("Unrealized P&L",  cents_str(open_upnl))
    with p3: st.metric("Total exposure",  usd(exposure))
    with p4:
        if next_close_days is not None:
            st.metric("Next expiry", f"{next_close_days:.0f} days",
                      delta="SOON" if next_close_days <= 7 else None,
                      delta_color="inverse" if next_close_days <= 7 else "normal")
        else:
            st.metric("Next expiry", "—")

    st.divider()

    if positions:
        # Sort by close_time ascending (soonest first)
        sorted_pos = sorted(
            positions.items(),
            key=lambda kv: kv[1].get("close_time") or "9999",
        )

        # ── Main positions table ───────────────────────────────────────────────
        rows = []
        for ticker, pos in sorted_pos:
            upnl       = pos.get("_upnl")
            days_left  = _days_until(pos.get("close_time"))
            days_held_ = _days_held(pos.get("entered_at"))

            # Entry date (date only, time in tooltip via sub-text)
            entered_dt = _parse_dt(pos.get("entered_at"))
            entered_s  = entered_dt.strftime("%-d %b %Y") if entered_dt else "—"
            entered_t  = entered_dt.strftime("%H:%M UTC")  if entered_dt else ""

            close_dt   = _parse_dt(pos.get("close_time"))
            close_s    = close_dt.strftime("%-d %b %Y")   if close_dt else "—"
            close_t    = close_dt.strftime("%H:%M UTC")    if close_dt else ""

            hwm_pnl     = pos.get("high_water_pnl")
            tranche_done = pos.get("tranche_done", 0)
            fv_entry    = pos.get("fair_value")

            rows.append({
                "Ticker":    ticker,
                "Side":      pos.get("side", "—").upper(),
                "Qty":       pos.get("contracts", 1),
                "Entry ¢":   pos.get("entry_cents", 0),
                "Cur ¢":     pos.get("_cur") or 0,
                "P&L ¢":     upnl if upnl is not None else float("nan"),
                "Peak P&L":  hwm_pnl if hwm_pnl is not None else float("nan"),
                "FV @entry": fv_entry if fv_entry is not None else float("nan"),
                "Tranche":   f"T{tranche_done}" if tranche_done else "—",
                "Entered":   f"{entered_s}  {entered_t}",
                "Closes":    f"{close_s}  {close_t}",
                "Days left": round(days_left, 1) if days_left is not None else float("nan"),
                "Held":      _held_str(days_held_),
                "Meeting":   pos.get("meeting", "—"),
            })

        df = pd.DataFrame(rows)

        def _colour_pnl(val):
            try:
                if pd.isna(val): return ""
                if val > 0:  return "color:#3fb950;font-weight:600"
                if val < 0:  return "color:#f85149;font-weight:600"
            except Exception:
                pass
            return ""

        def _colour_days(val):
            try:
                if pd.isna(val): return ""
                if val <= 0:  return "color:#f85149;font-weight:700"
                if val <= 7:  return "color:#f85149;font-weight:600"
                if val <= 30: return "color:#e3b341;font-weight:600"
                if val <= 90: return "color:#58a6ff"
                return "color:#79c0ff"
            except Exception:
                return ""

        styled = (
            df.style
            .map(_colour_pnl,  subset=["P&L ¢", "Peak P&L"])
            .map(_colour_days, subset=["Days left"])
            .format({
                "Entry ¢":   "{:.0f}¢",
                "Cur ¢":     lambda v: f"{v:.0f}¢" if v else "—",
                "P&L ¢":     lambda v: f"{v:+.0f}¢" if not pd.isna(v) else "—",
                "Peak P&L":  lambda v: f"{v:+.0f}¢" if not pd.isna(v) else "—",
                "FV @entry": lambda v: f"{v:.2f}" if not pd.isna(v) else "—",
                "Days left": lambda v: f"{v:.0f}" if not pd.isna(v) else "—",
            })
        )
        st.dataframe(styled, use_container_width=True, hide_index=True, height=min(38 * len(rows) + 38, 560))
        st.caption(
            "**FV @entry** = model fair-value probability at the time the signal was generated "
            "(FedWatch / FRED / log-normal). This is frozen at entry — it does not update with the market. "
            "**Peak P&L** = highest unrealized profit seen so far (trailing stop will exit if price "
            "retreats 12¢ from this peak).  **Tranche** = T1 means 50% of contracts already exited "
            "at the first pre-expiry window."
        )

        # ── Timeline view grouped by FOMC meeting ─────────────────────────────
        st.divider()
        st.markdown("### Timeline by meeting")

        meetings: dict = {}
        for ticker, pos in sorted_pos:
            meeting = pos.get("meeting") or pos.get("asset_class", ticker)
            if meeting not in meetings:
                meetings[meeting] = {"tickers": [], "close_time": pos.get("close_time"), "days": _days_until(pos.get("close_time"))}
            meetings[meeting]["tickers"].append((ticker, pos))

        for meeting, m in meetings.items():
            days   = m["days"]
            close  = _fmt_close(m["close_time"])

            if days is None:
                urgency = "badge-cyan"
            elif days <= 0:
                urgency = "badge-red"
            elif days <= 7:
                urgency = "badge-red"
            elif days <= 30:
                urgency = "badge-orange"
            elif days <= 90:
                urgency = "badge-blue"
            else:
                urgency = "badge-cyan"

            days_label = f"{days:.0f}d" if days is not None and days > 0 else ("PAST" if days is not None else "—")

            st.markdown(
                f"<div style='background:#161b22;border:1px solid #21262d;border-radius:8px;"
                f"padding:12px 16px;margin-bottom:8px;'>"
                f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;'>"
                f"<span style='font-weight:700;color:#e6edf3;font-size:14px'>{meeting}</span>"
                f"<span class='badge {urgency}'>{days_label}</span>"
                f"<span style='font-size:12px;color:#8b949e;margin-left:4px'>Resolves {close}</span>"
                f"</div>"
                f"<div style='display:flex;flex-wrap:wrap;gap:6px'>",
                unsafe_allow_html=True,
            )

            for ticker, pos in m["tickers"]:
                upnl  = pos.get("_upnl")
                side  = pos.get("side", "yes")
                qty   = pos.get("contracts", 1)
                ent   = pos.get("entry_cents", 0)
                pnl_color = "#3fb950" if (upnl or 0) >= 0 else "#f85149"
                pnl_s     = f"{upnl:+.0f}¢" if upnl is not None else "—"
                side_color = "#3fb950" if side == "yes" else "#e3b341"
                # Extract just the strike from ticker (e.g. T3.25 from KXFED-26DEC-T3.25)
                parts  = ticker.split("-")
                strike = parts[-1] if len(parts) >= 3 else ticker

                st.markdown(
                    f"<div style='background:#0d1117;border:1px solid #30363d;border-radius:6px;"
                    f"padding:6px 10px;font-size:12px;font-family:monospace;'>"
                    f"<span style='color:#8b949e'>{strike}</span> &nbsp;"
                    f"<span style='color:{side_color};font-weight:600'>{side.upper()}</span> "
                    f"×{qty} &nbsp;"
                    f"<span style='color:#8b949e'>@{ent}¢</span> &nbsp;"
                    f"<span style='color:{pnl_color};font-weight:600'>{pnl_s}</span>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

            st.markdown("</div></div>", unsafe_allow_html=True)

        # ── P&L breakdown bars ─────────────────────────────────────────────────
        positions_with_pnl = [(t, p) for t, p in sorted_pos if p.get("_upnl") is not None]
        if positions_with_pnl:
            st.divider()
            st.markdown("### P&L breakdown")
            for ticker, pos in positions_with_pnl:
                upnl  = pos["_upnl"]
                color = "#3fb950" if upnl >= 0 else "#f85149"
                cl, cm, cr = st.columns([2, 6, 1])
                with cl: st.caption(ticker[:28])
                with cm:
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
            use_container_width=True,
            hide_index=True,
            height=400,
        )
    else:
        st.info("No fills in the execution stream. Fills appear as the Exec node processes signals.")

    if rejects:
        with st.expander(f"Rejected signals ({len(rejects)})"):
            # Reason breakdown bar chart — quick visual on what's blocking
            reason_counts: dict = {}
            for rej in rejects:
                r = rej.get("reject_reason") or "UNKNOWN"
                reason_counts[r] = reason_counts.get(r, 0) + 1
            if len(reason_counts) > 1:
                rej_df = pd.DataFrame(
                    sorted(reason_counts.items(), key=lambda x: -x[1]),
                    columns=["Reason", "Count"],
                )
                st.bar_chart(rej_df.set_index("Reason")["Count"], height=160)

            rej_rows = [{
                "Time":   hms(rej.get("ts_us")),
                "Ticker": rej.get("ticker",        "—"),
                "Side":   (rej.get("side") or "—").upper(),
                "Reason": rej.get("reject_reason", "—"),
            } for rej in rejects]
            st.dataframe(pd.DataFrame(rej_rows), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# CONTROLS
# ═══════════════════════════════════════════════════════════════════════════════

with t_ctrl:
    config    = d["config"]
    is_halted = d["is_halted"]

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _env_float(key: str, default: float) -> float:
        try:   return float(os.getenv(key, str(default)))
        except: return default

    def _env_int(key: str, default: int) -> int:
        try:   return int(os.getenv(key, str(default)))
        except: return default

    # ── Emergency ─────────────────────────────────────────────────────────────
    st.markdown(
        "<div class='settings-card'><div class='settings-card-header'>"
        "Emergency controls"
        "</div></div>",
        unsafe_allow_html=True,
    )
    ec1, ec2 = st.columns([1, 3])
    with ec1:
        if is_halted:
            if st.button("▶ Resume trading", type="primary", use_container_width=True):
                r.hset("ep:config", mapping={"HALT_TRADING": "0", "llm_halt_trading": "0"})
                time.sleep(0.2); st.rerun()
        else:
            if st.button("🛑 Halt all trading", use_container_width=True):
                r.hset("ep:config", mapping={"HALT_TRADING": "1", "llm_halt_trading": "1"})
                time.sleep(0.2); st.rerun()
    with ec2:
        if is_halted:
            st.error("Trading is **halted** — new signals are dropped. Click Resume to restart.")
        else:
            st.success("Trading is **active** — signals are flowing normally.")

    st.divider()

    # ── Trading Parameters ────────────────────────────────────────────────────
    st.markdown("### Trading Parameters")
    tp1, tp2, tp3 = st.columns(3)

    env_edge = _env_float("KALSHI_EDGE_THRESHOLD", 0.10)
    env_maxc = _env_int("KALSHI_MAX_CONTRACTS", 10)
    env_conf = _env_float("KALSHI_MIN_CONFIDENCE", 0.60)

    with tp1:
        cur_edge = float(config.get("override_edge_threshold", env_edge))
        v_edge   = st.slider("Edge threshold", 0.05, 0.30, cur_edge, step=0.01,
                             help="Minimum edge required to publish a signal.")
        st.caption(f".env default: {env_edge:.2f}")
        if st.button("Apply edge threshold", key="k_edge"):
            r.hset("ep:config", "override_edge_threshold", str(v_edge))
            st.success(f"Edge threshold → {v_edge:.2f}")

    with tp2:
        cur_maxc = int(float(config.get("override_max_contracts", env_maxc)))
        v_maxc   = st.slider("Max contracts", 1, 20, cur_maxc, step=1,
                             help="Maximum contracts per signal.")
        st.caption(f".env default: {env_maxc}")
        if st.button("Apply max contracts", key="k_maxc"):
            r.hset("ep:config", "override_max_contracts", str(int(v_maxc)))
            st.success(f"Max contracts → {int(v_maxc)}")

    with tp3:
        cur_conf = float(config.get("override_min_confidence", env_conf))
        v_conf   = st.slider("Min confidence", 0.40, 0.90, cur_conf, step=0.05,
                             help="Minimum model confidence to publish a signal.")
        st.caption(f".env default: {env_conf:.2f}")
        if st.button("Apply min confidence", key="k_conf"):
            r.hset("ep:config", "override_min_confidence", str(v_conf))
            st.success(f"Min confidence → {v_conf:.2f}")

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
            st.success(f"Scale → {val:.2f}x")

    with ps2:
        cur = min(float(config.get("llm_kelly_fraction", "0.25")), 0.40)
        val = st.slider("Kelly fraction", 0.05, 0.40, cur, step=0.01,
                        help="Fraction of Kelly criterion to trade. 0.25 = quarter-Kelly.")
        if st.button("Apply Kelly", key="k_kelly"):
            r.hset("ep:config", "llm_kelly_fraction", str(val))
            st.success(f"Kelly → {val:.2f}")

    st.divider()

    # ── Exit Management ───────────────────────────────────────────────────────
    st.markdown("### Exit Management")
    ex1, ex2, ex3 = st.columns(3)

    env_tp  = _env_int("KALSHI_TAKE_PROFIT_CENTS", 20)
    env_sl  = _env_int("KALSHI_STOP_LOSS_CENTS", 15)
    env_hbc = _env_float("KALSHI_HOURS_BEFORE_CLOSE", 24.0)

    with ex1:
        cur_tp = int(float(config.get("override_take_profit_cents", env_tp)))
        v_tp   = st.slider("Take profit (¢)", 5, 50, cur_tp, step=1,
                           help="Exit when position gain reaches this many cents.")
        st.caption(f".env default: {env_tp}¢")
        if st.button("Apply take profit", key="k_tp"):
            r.hset("ep:config", "override_take_profit_cents", str(int(v_tp)))
            st.success(f"Take profit → {int(v_tp)}¢")

    with ex2:
        cur_sl = int(float(config.get("override_stop_loss_cents", env_sl)))
        v_sl   = st.slider("Stop loss (¢)", 5, 40, cur_sl, step=1,
                           help="Exit when position loss reaches this many cents.")
        st.caption(f".env default: {env_sl}¢")
        if st.button("Apply stop loss", key="k_sl"):
            r.hset("ep:config", "override_stop_loss_cents", str(int(v_sl)))
            st.success(f"Stop loss → {int(v_sl)}¢")

    with ex3:
        cur_hbc = float(config.get("override_hours_before_close", env_hbc))
        v_hbc   = st.slider("Hours before close", 1, 48, int(cur_hbc), step=1,
                            help="Exit positions this many hours before market closes.")
        st.caption(f".env default: {env_hbc:.0f}h")
        if st.button("Apply hours before close", key="k_hbc"):
            r.hset("ep:config", "override_hours_before_close", str(float(v_hbc)))
            st.success(f"Hours before close → {v_hbc}h")

    st.divider()

    # ── Strategy toggles ───────────────────────────────────────────────────────
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

    # ── BTC signal thresholds ──────────────────────────────────────────────────
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

    # ── Risk Limits ────────────────────────────────────────────────────────────
    st.markdown("### Risk Limits")
    rl1, rl2, rl3 = st.columns(3)

    env_mte = int(_env_float("KALSHI_MAX_TOTAL_EXPOSURE", 0.30) * 100)
    env_mme = int(_env_float("KALSHI_MAX_MARKET_EXPOSURE", 0.05) * 100)
    env_ddl = int(_env_float("KALSHI_DAILY_DRAWDOWN_LIMIT", 0.10) * 100)

    with rl1:
        cur_mte = int(float(config.get("override_max_total_exposure", env_mte)))
        v_mte   = st.slider("Max total exposure (%)", 10, 60, cur_mte, step=5,
                            help="Maximum total portfolio exposure across all positions.")
        st.caption(f".env default: {env_mte}%")
        if st.button("Apply total exposure", key="k_mte"):
            r.hset("ep:config", "override_max_total_exposure", str(v_mte))
            st.success(f"Max total exposure → {v_mte}%")

    with rl2:
        cur_mme = int(float(config.get("override_max_market_exposure", env_mme)))
        v_mme   = st.slider("Max market exposure (%)", 2, 20, cur_mme, step=1,
                            help="Maximum exposure in any single market.")
        st.caption(f".env default: {env_mme}%")
        if st.button("Apply market exposure", key="k_mme"):
            r.hset("ep:config", "override_max_market_exposure", str(v_mme))
            st.success(f"Max market exposure → {v_mme}%")

    with rl3:
        cur_ddl = int(float(config.get("override_daily_drawdown_limit", env_ddl)))
        v_ddl   = st.slider("Daily drawdown limit (%)", 5, 40, cur_ddl, step=5,
                            help="Halt trading if daily loss exceeds this percentage.")
        st.caption(f".env default: {env_ddl}%")
        if st.button("Apply drawdown limit", key="k_ddl"):
            r.hset("ep:config", "override_daily_drawdown_limit", str(v_ddl))
            st.success(f"Daily drawdown limit → {v_ddl}%")

    st.divider()

    # ── Fed Rate ───────────────────────────────────────────────────────────────
    st.markdown("### Fed Rate")

    # Show current value — prefer Redis, then env fallback
    _redis_rate   = config.get("CURRENT_FED_RATE")
    _env_rate_str = os.getenv("CURRENT_FED_RATE", "4.25")
    _env_rate     = float(_env_rate_str)

    if _redis_rate:
        st.markdown(
            f"Current rate (Redis override): "
            f"<span class='badge badge-orange'>{float(_redis_rate):.2f}%</span>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"Current rate (.env / FRED): "
            f"<span class='badge badge-cyan'>{_env_rate:.2f}%</span>",
            unsafe_allow_html=True,
        )

    fr1, fr2 = st.columns([1, 2])
    with fr1:
        fed_rate_val = st.number_input(
            "Manual fed rate override (%)",
            min_value=0.0,
            max_value=10.0,
            value=float(_redis_rate) if _redis_rate else _env_rate,
            step=0.25,
            format="%.2f",
            help="Writes CURRENT_FED_RATE to ep:config. Intel reads this next cycle.",
        )
    with fr2:
        st.caption(
            "Auto-fetched from FRED daily. Override only if FRED data is stale. "
            "Clear the override to resume auto-fetch."
        )
        fc1, fc2 = st.columns(2)
        with fc1:
            if st.button("Apply fed rate", key="k_fedrate"):
                r.hset("ep:config", "CURRENT_FED_RATE", str(fed_rate_val))
                st.success(f"CURRENT_FED_RATE → {fed_rate_val:.2f}%")
        with fc2:
            if st.button("Clear fed rate override", key="k_fedrate_clear"):
                r.hdel("ep:config", "CURRENT_FED_RATE")
                st.success("CURRENT_FED_RATE override cleared — FRED auto-fetch resumes.")
                time.sleep(0.2); st.rerun()

    st.divider()

    # ── LLM policy ─────────────────────────────────────────────────────────────
    st.markdown("### Claude LLM policy")

    last_run = config.get("llm_last_run_ts")
    run_str  = ago(int(last_run) * 1_000_000) if last_run else "never"
    notes    = config.get("llm_notes", "—")

    # LLM notes card with purple left border
    st.markdown(
        f"<div class='llm-card'>"
        f"<div class='llm-label'>LLM Notes</div>"
        f"<div class='llm-notes'>{notes}</div>"
        f"<div class='llm-meta'>Last run: {run_str}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    llm_keys = sorted(k for k in config if k.startswith("llm_") and k not in ("llm_notes", "llm_last_run_ts"))
    if llm_keys:
        st.dataframe(
            pd.DataFrame([{"Parameter": k[4:], "Value": config[k]} for k in llm_keys]),
            use_container_width=True,
            hide_index=True,
            height=min(38 * len(llm_keys) + 38, 280),
        )

    lc1, lc2 = st.columns([1, 1])
    with lc1:
        if st.button("▶ Run LLM now", use_container_width=True,
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
        if st.button("Clear LLM overrides", use_container_width=True,
                     help="Delete all llm_* keys from ep:config, reverting to .env defaults."):
            keys_to_del = [k for k in r.hkeys("ep:config") if k.startswith("llm_")]
            if keys_to_del:
                r.hdel("ep:config", *keys_to_del)
            st.success(f"Cleared {len(keys_to_del)} override(s).")
            time.sleep(0.2); st.rerun()

    with st.expander("Raw ep:config"):
        st.json(config)


# ── Auto-refresh ───────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(REFRESH_S)
    st.rerun()
