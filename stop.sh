#!/bin/bash
pkill -f "kalshi_bot.py" 2>/dev/null
pkill -f "streamlit" 2>/dev/null
screen -wipe 2>/dev/null
echo "✅ All stopped"
