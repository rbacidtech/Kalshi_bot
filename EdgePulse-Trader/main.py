#!/usr/bin/env python3
from kalshi_python import Configuration, KalshiClient
import time, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', 
                   handlers=[logging.FileHandler('trader.log')])
log = logging.getLogger("EdgePulse")

print("🚀 EdgePulse-Trader LIVE TRADING")
log.info("Bot started")

config = Configuration()
config.api_key_id = "aef3bec8-015d-462d-8f7f-0935b1032366"
with open("private_key.pem", "r") as f:
    config.private_key_pem = f.read().strip()

kalshi = KalshiClient(config)
balance = kalshi.get_balance()
log.info(f"Balance: ${balance.balance/100:.2f}")

while True:
    markets = kalshi.get_markets(status="open", limit=200).markets
    log.info(f"Scanned {len(markets)} markets")
    time.sleep(300)  # Scan every 5 minutes
