import os, json, time, logging
from pathlib import Path
from datetime import datetime
import requests
import streamlit as st
import pandas as pd
from dotenv import load_dotenv

ROOT         = Path(__file__).resolve().parent.parent
ENV_PATH     = ROOT / ".env"
TRADES_CSV   = ROOT / "output" / "trades.csv"
ADVISOR_TXT  = ROOT / "output" / "advisor_recommendations.txt"
SIGNALS_JSON = Path(__file__).resolve().parent / "signals.json"
FLASK_URL    = "http://localhost:5050"

load_dotenv(ENV_PATH)
logging.getLogger("urllib3").setLevel(logging.WARNING)

st.set_page_config(page_title="EdgePulse Command Center",page_icon="🚀",layout="wide",initial_sidebar_state="expanded")

st.markdown("""<style>
div[data-testid="metric-container"]{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:14px 18px;}
</style>""",unsafe_allow_html=True)

@st.cache_data(ttl=10)
def fetch_bot_state():
    try:
        r=requests.get(f"{FLASK_URL}/api/state",timeout=2)
        if r.status_code==200:return r.json()
    except Exception:pass
    return {}

@st.cache_data(ttl=15)
def load_trades():
    if not TRADES_CSV.exists():return pd.DataFrame()
    try:
        df=pd.read_csv(TRADES_CSV)
        if "timestamp" in df.columns:df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce")
        for col in ("price_cents","contracts","pnl_cents"):
            if col in df.columns:df[col]=pd.to_numeric(df[col],errors="coerce")
        return df
    except Exception:return pd.DataFrame()

@st.cache_data(ttl=30)
def load_env_settings():
    settings={}
    if not ENV_PATH.exists():return settings
    SKIP={"KEY","PEM","TOKEN","SECRET","SID","PASSWORD","SMTP"}
    for line in ENV_PATH.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:continue
        k,_,v=line.partition("=")
        k=k.strip()
        if not any(s in k.upper() for s in SKIP):settings[k]=v.strip()
    return settings

@st.cache_data(ttl=30)
def load_advisor():
    if not ADVISOR_TXT.exists():return {}
    recs={}
    for line in ADVISOR_TXT.read_text().splitlines():
        if line.startswith("#") or "=" not in line:continue
        k,_,v=line.partition("=")
        recs[k.strip()]=v.strip()
    return recs

@st.cache_data(ttl=10)
def load_edgepulse_signals():
    if not SIGNALS_JSON.exists():return []
    try:
        data=json.loads(SIGNALS_JSON.read_text())
        return data if isinstance(data,list) else []
    except Exception:return []

def c2d(cents):
    try:return float(cents)/100
    except (TypeError,ValueError):return 0.0

def build_pnl_curve(df):
    if df.empty or "pnl_cents" not in df.columns:return pd.DataFrame()
    r=df[df["pnl_cents"].notna()&(df["pnl_cents"]!=0)].copy()
    if r.empty or "timestamp" not in r.columns:return pd.DataFrame()
    r=r.sort_values("timestamp")
    r["P&L ($)"]=r["pnl_cents"].cumsum()/100
    return r[["timestamp","P&L ($)"]].set_index("timestamp")

def edge_highlight(val):
    try:
        v=float(val)
        if v>=15:return "background-color:#14532d;color:#86efac"
        if v>=10:return "background-color:#713f12;color:#fde68a"
        if v<0:return "color:#ef4444"
    except (TypeError,ValueError):pass
    return ""

with st.sidebar:
    st.markdown("## EdgePulse Controls")
    auto_refresh=st.toggle("Auto-refresh (10s)",value=True)
    if st.button("Refresh Now",use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    state=fetch_bot_state()
    bot_online=bool(state)
    st.success("Main bot: Online") if bot_online else st.error("Main bot: Offline")
    ep_signals=load_edgepulse_signals()
    st.success(f"EdgePulse: {len(ep_signals)} signals") if ep_signals else st.warning("EdgePulse: No signals yet")
    st.divider()
    st.code("python kalshi_bot.py",language="bash")
    st.code("python advisor.py --mode paper",language="bash")

trades_df=load_trades()
settings=load_env_settings()
advisor=load_advisor()
mode_raw=state.get("mode","paper")
is_live=mode_raw=="live"
ws_ok=state.get("ws_connected",False)
balance=c2d(state.get("balance_cents",0))
session_pnl=c2d(state.get("session_pnl",0))
unreal_pnl=c2d(state.get("unrealized_pnl",0))
cycle_count=state.get("cycle_count",0)
signals_raw=state.get("signals") or []
markets_raw={k:v for k,v in (state.get("markets") or {}).items() if "KXFED" in k or "FOMC" in k}
positions_raw=state.get("positions") or {}
recent_trades_raw=state.get("recent_trades") or []

st.markdown("# 🚀 EdgePulse Command Center")
st.caption(f"{'🔴 LIVE' if is_live else '🟡 PAPER'}  |  {'✅ WS Connected' if ws_ok else '⚠️ WS Degraded'}  |  Updated {datetime.now().strftime('%H:%M:%S')}")
if not bot_online:st.error("Bot offline — start with: python kalshi_bot.py")

k1,k2,k3,k4,k5,k6=st.columns(6)
k1.metric("💰 Balance",f"${balance:.2f}" if bot_online else "—")
k2.metric("📈 Session P&L",f"${session_pnl:+.2f}" if bot_online else "—",delta=f"${session_pnl:+.2f}" if session_pnl!=0 else None)
k3.metric("📊 Unrealized",f"${unreal_pnl:+.2f}" if bot_online else "—")
k4.metric("🔄 Cycles",cycle_count if bot_online else "—")
k5.metric("🎯 Signals",len(signals_raw) if bot_online else "—")
k6.metric("💼 All-Time P&L",f"${trades_df['pnl_cents'].sum()/100:+.2f}" if not trades_df.empty and "pnl_cents" in trades_df.columns else "$0.00")
st.divider()

left,right=st.columns([3,2],gap="medium")

with left:
    st.markdown("### 🎯 Live Signal Feed")
    if signals_raw:
        sig_df=pd.DataFrame(signals_raw)
        cols=[c for c in ["ticker","side","market_price","fair_value","edge","confidence","contracts"] if c in sig_df.columns]
        st.dataframe(sig_df[cols].style.map(edge_highlight,subset=["edge"]) if "edge" in sig_df.columns else sig_df[cols],use_container_width=True,height=180)
    else:
        st.info("No signals this cycle — watching 50 FOMC markets for edge >= 10c")

    st.markdown("### 📡 FOMC Market Intelligence")
    if markets_raw:
        rows=[]
        for ticker,m in markets_raw.items():
            parts=ticker.split("-")
            short="-".join(parts[-2:]) if len(parts)>=2 else ticker
            rows.append({"Ticker":short,"Price(c)":m.get("last_price",0),"Fair(c)":m.get("fair_value") or "—","Edge(c)":m.get("edge",0),"Conf%":round((m.get("confidence") or 0)*100),"Spread":m.get("spread",0),"Updated":datetime.fromisoformat(m["updated_at"]).strftime("%H:%M:%S") if m.get("updated_at") else "—"})
        mkt_df=pd.DataFrame(rows).sort_values("Edge(c)",ascending=False)
        st.dataframe(mkt_df.style.map(edge_highlight,subset=["Edge(c)"]),use_container_width=True,height=300)
    else:
        st.info("Market table populates after first bot scan.")

    st.markdown("### 🧠 EdgePulse Intelligence")
    if ep_signals:
        st.dataframe(pd.DataFrame(ep_signals),use_container_width=True,height=200)
        st.caption(f"Last scan: {ep_signals[0].get('timestamp','—')}")
    else:
        st.info("EdgePulse feed will appear once the EdgePulse VPS is running.")

    st.markdown("### 📈 Trade History")
    if not trades_df.empty:
        entries=trades_df[trades_df["action"]=="entry"] if "action" in trades_df.columns else trades_df
        resolved=trades_df[trades_df["pnl_cents"].notna()&(trades_df["pnl_cents"]!=0)] if "pnl_cents" in trades_df.columns else pd.DataFrame()
        tc1,tc2,tc3,tc4=st.columns(4)
        tc1.metric("Entries",len(entries))
        if not resolved.empty:
            tc2.metric("Hit Rate",f"{(resolved['pnl_cents']>0).sum()/len(resolved):.1%}")
            tc3.metric("Total P&L",f"${resolved['pnl_cents'].sum()/100:+.2f}")
            tc4.metric("Resolved",len(resolved))
            pnl_curve=build_pnl_curve(trades_df)
            if not pnl_curve.empty:st.line_chart(pnl_curve,height=200)
        else:
            tc2.metric("Hit Rate","—");tc3.metric("Total P&L","—");tc4.metric("Resolved","0")
            st.caption("P&L chart appears after FOMC meetings resolve trades.")
        with st.expander("Full trade log",expanded=False):
            st.dataframe(trades_df.sort_values("timestamp",ascending=False).head(200) if "timestamp" in trades_df.columns else trades_df.tail(200),use_container_width=True)
    else:
        st.info("No trades yet — bot is paper trading.")

with right:
    st.markdown("### 📂 Open Positions")
    if positions_raw:
        pos_rows=[]
        for ticker,p in positions_raw.items():
            parts=ticker.split("-")
            short="-".join(parts[-2:]) if len(parts)>=2 else ticker
            pos_rows.append({"Ticker":short,"Side":p.get("side","").upper(),"Qty":p.get("contracts",0),"Entry(c)":p.get("entry_cents",0),"Unreal P&L":f"${c2d(p.get('unrealized_pnl',0)):+.2f}"})
        st.dataframe(pd.DataFrame(pos_rows),use_container_width=True)
    else:
        st.caption("No open positions.")

    st.markdown("### 🕐 Recent Trades")
    if recent_trades_raw:
        rt_rows=[]
        for t in list(reversed(recent_trades_raw))[:15]:
            try:ts=datetime.fromisoformat(t["timestamp"]).strftime("%H:%M:%S")
            except Exception:ts=str(t.get("timestamp",""))
            parts=t.get("ticker","").split("-")
            short="-".join(parts[-2:]) if len(parts)>=2 else t.get("ticker","")
            rt_rows.append({"Time":ts,"Action":t.get("action","").upper(),"Ticker":short,"Side":t.get("side",""),"Qty":t.get("contracts",0),"Price":f"{t.get('price',0)}c"})
        st.dataframe(pd.DataFrame(rt_rows),use_container_width=True,height=250)
    else:
        st.caption("Trade feed populates on first signal.")

    st.divider()
    st.markdown("### ⚠️ Risk Monitor")
    r1,r2=st.columns(2)
    r1.metric("Max Contracts",settings.get("KALSHI_MAX_CONTRACTS","5"))
    r2.metric("Edge Threshold",f"{float(settings.get('KALSHI_EDGE_THRESHOLD','0.10')):.0%}")
    r3,r4=st.columns(2)
    r3.metric("Min Confidence",f"{float(settings.get('KALSHI_MIN_CONFIDENCE','0.60')):.0%}")
    r4.metric("Kelly Fraction",f"{float(settings.get('KALSHI_KELLY_FRACTION','0.25')):.0%}")
    r5,r6=st.columns(2)
    r5.metric("Take Profit",f"{settings.get('KALSHI_TAKE_PROFIT_CENTS','20')}c")
    r6.metric("Stop Loss",f"{settings.get('KALSHI_STOP_LOSS_CENTS','15')}c")
    r7,r8=st.columns(2)
    r7.metric("Drawdown Limit",f"{float(settings.get('KALSHI_DAILY_DRAWDOWN_LIMIT','0.10')):.0%}")
    r8.metric("Mode","🔴 LIVE" if is_live else "🟡 PAPER")

    st.divider()
    st.markdown("### 🧮 Advisor")
    if advisor:
        WATCH=[("KALSHI_EDGE_THRESHOLD","Edge Threshold"),("KALSHI_MIN_CONFIDENCE","Min Confidence"),("KALSHI_MAX_CONTRACTS","Max Contracts"),("KALSHI_KELLY_FRACTION","Kelly Fraction"),("KALSHI_TAKE_PROFIT_CENTS","Take Profit"),("KALSHI_STOP_LOSS_CENTS","Stop Loss"),("KALSHI_DAILY_DRAWDOWN_LIMIT","Drawdown Limit"),("KALSHI_PAPER_TRADE","Paper Mode")]
        changed=False
        for key,label in WATCH:
            rec=advisor.get(key);cur=settings.get(key,"—")
            if rec and rec!=cur:st.markdown(f"🔶 **{label}**: `{cur}` → `{rec}`");changed=True
        if not changed:st.success("All settings match advisor ✅")
        st.caption("Run python advisor.py to refresh.")
    else:
        st.info("Run python advisor.py --mode paper")

    with st.expander("⚙️ Current .env Settings",expanded=False):
        for k,v in settings.items():st.text(f"{k} = {v}")

st.divider()
f1,f2,f3,f4=st.columns(4)
f1.caption(f"🤖 Cycles: {cycle_count}")
f2.caption(f"📡 Markets: {len(markets_raw)}")
f3.caption(f"📋 Trades: {len(trades_df) if not trades_df.empty else 0}")
f4.caption(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if auto_refresh:
    time.sleep(10)
    st.cache_data.clear()
    st.rerun()
