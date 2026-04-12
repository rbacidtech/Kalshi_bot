#!/bin/bash
echo "🛑 Stopping all existing sessions..."
pkill -f "kalshi_bot.py" 2>/dev/null
pkill -f "streamlit" 2>/dev/null
pkill -f "screen" 2>/dev/null
sleep 3

echo "🚀 Starting Kalshi Bot..."
cd ~/Kalshi_bot
source .venv/bin/activate
screen -dmS bot bash -c 'cd ~/Kalshi_bot && source .venv/bin/activate && python3 kalshi_bot.py 2>&1 | tee output/bot.log'
screen -dmS dash bash -c 'cd ~/Kalshi_bot && source .venv/bin/activate && streamlit run EdgePulse-Trader/dashboard.py --server.port 8501 --server.address 0.0.0.0'

sleep 2
echo ""
echo "✅ Running sessions:"
screen -ls
echo ""
echo "📊 Dashboard: http://167.71.27.43:8501"
echo "🤖 Bot logs:  screen -r bot"
echo "📈 Dash logs: screen -r dash"
