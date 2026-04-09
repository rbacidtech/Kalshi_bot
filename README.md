# Kalshi Trading Bot

A Python trading bot for [Kalshi](https://kalshi.com) — the only CFTC-regulated
prediction market in the United States. Mirrors the Polymarket bot structure
but works legally for US traders.

---

## Quick Start (one copy-paste block)

```bash
# 1. Create project directory and enter it
mkdir kalshi_bot && cd kalshi_bot

# 2. Create Python virtual environment
python3 -m venv .venv

# 3. Activate it
source .venv/bin/activate

# 4. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 5. Create .env from example
cp .env.example .env

# 6. Edit .env with your credentials
nano .env

# 7. First run — paper trade (safe, no real money)
python kalshi_bot.py

# 8. Check trade log
cat output/trades.csv

# 9. When ready, run live (after verifying .env)
KALSHI_PAPER_TRADE=false python kalshi_bot.py
```

---

## Getting API Credentials

1. Log in to [kalshi.com](https://kalshi.com)
2. Go to **Account Settings → Developer → API Keys**
3. Click **Generate New API Key**
4. Copy your **Key ID** → paste into `.env` as `KALSHI_API_KEY_ID`
5. Download the **private_key.pem** → place it in the same folder as the bot

> ⚠️ Never commit your `.env` or `private_key.pem` to git.
> A `.gitignore` is included to prevent this.

---

## How It Works

| Stage | What it does |
|-------|-------------|
| **Market Scanner** | Fetches all open Kalshi markets (filtered by series if set) |
| **Edge Calculator** | Estimates fair value from order book mid-price, compares to last trade price |
| **Paper Simulator** | Logs trade signals to `output/trades.csv` — no real money |
| **Live Executor** | Places real limit orders via the Kalshi REST API v2 |

### Key Kalshi differences from Polymarket

- **Prices are in cents** (integers 0–100), not decimals. `65` means `$0.65` / 65% probability.
- **Auth uses RSA-PSS signatures** — every request is signed with your private key.
- **Demo environment** available at `demo-api.kalshi.co` — use it first!
- **CFTC regulated** — legal for US residents, no VPN needed.

---

## Strategy Tuning (`.env` knobs)

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_EDGE_THRESHOLD` | `0.05` | Min edge to trade (5 cents / 5%) |
| `KALSHI_MAX_CONTRACTS` | `10` | Max contracts per order |
| `KALSHI_POLL_INTERVAL` | `60` | Seconds between market scans |
| `KALSHI_SERIES_FILTER` | *(blank)* | Filter by series (e.g. `INXD`) |

---

## Output

All trades are logged to `output/trades.csv`:

```
timestamp,ticker,side,contracts,price_cents,fair_value,edge,mode
2026-04-05T12:00:00,INXD-26APR05-B5000,yes,3,42,0.47,0.05,paper
```

---

## ⚠️ Risk Warning

- Always start with `KALSHI_PAPER_TRADE=true`
- Start with small `KALSHI_MAX_CONTRACTS` (1–5) in live mode
- Fees can erode small edges — factor them in before going live
- Past performance of any strategy is not indicative of future results
