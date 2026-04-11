#!/usr/bin/env python3
from kalshi_python import Configuration, KalshiClient

config = Configuration()
config.api_key_id = "aef3bec8-015d-462d-8f7f-0935b1032366"
with open("private_key.pem", "r") as f:
    config.private_key_pem = f.read().strip()
kalshi = KalshiClient(config)

balance = kalshi.get_balance()
print(f"💰 Balance: ${balance.balance / 100:.2f}")
print("✅ Stats working!")
print("📈 Bot logs: tail -f trader.log")
