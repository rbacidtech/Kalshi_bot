"""
portfolio.py — Portfolio reporting (balance + open positions).

Kept in its own module so it can be called independently
or silenced without touching bot logic.
"""

import logging

log = logging.getLogger(__name__)


def print_summary(client, balance_cents: int = None) -> None:
    """
    Log current balance and up to 10 open positions.

    Args:
        client:         KalshiClient
        balance_cents:  If provided, skips the /portfolio/balance REST call
                        (avoids redundant fetch when caller already has balance).

    Failures are logged at WARNING and do not propagate —
    a reporting hiccup should never halt the trading loop.
    """
    _log_balance(client, balance_cents)
    _log_positions(client)


def _log_balance(client, balance_cents: int = None) -> None:
    try:
        if balance_cents is None:
            data          = client.get("/portfolio/balance")
            balance_cents = data.get("balance", 0)
        log.info("Balance: $%.2f", balance_cents / 100)
    except Exception as exc:
        log.warning("Could not fetch balance: %s", exc)


def _log_positions(client) -> None:
    try:
        data      = client.get("/portfolio/positions", params={"limit": 50})
        positions = data.get("market_positions", [])

        if not positions:
            log.info("No open positions.")
            return

        log.info("Open positions (%d):", len(positions))
        for p in positions[:10]:
            log.info(
                "  %-40s  position=%+d  resting_orders=%d",
                p.get("market_ticker", "")[:40],
                p.get("position", 0),
                p.get("resting_orders_count", 0),
            )

        if len(positions) > 10:
            log.info("  ... and %d more.", len(positions) - 10)

    except Exception as exc:
        log.warning("Could not fetch positions: %s", exc)
