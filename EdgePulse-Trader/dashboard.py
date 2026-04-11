import streamlit as st
from kalshi_python import Configuration, KalshiClient

st.title("🚀 EdgePulse Live Dashboard")
st.markdown("**Real-time Kalshi trading stats**")

config = Configuration()
config.api_key_id = "aef3bec8-015d-462d-8f7f-0935b1032366"
with open("private_key.pem", "r") as f:
    config.private_key_pem = f.read().strip()
kalshi = KalshiClient(config)

bal = kalshi.get_balance()
st.metric("💰 Account Balance", f"${bal.balance/100:.2f}")

# Bot logs (safe)
try:
    with open('trader.log', 'r') as f:
        logs = f.readlines()[-20:]
    st.subheader("📊 Recent Bot Activity")
    st.code("".join(logs))
except FileNotFoundError:
    st.info("🚀 Bot running - trader.log will appear with trades")
except:
    st.warning("Log access issue")

st.markdown("---")
st.caption("🔄 Auto-refreshes every 10s | Bot: `systemctl status edgepulse`")
