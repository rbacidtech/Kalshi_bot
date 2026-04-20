"""
ep_adapters.py — Translate between kalshi_bot.strategy.Signal and SignalMessage.

This is the only translation layer — Strategy code itself is untouched.
"""

from typing import List

from ep_config import cfg, log
from kalshi_bot.strategy import Signal
from ep_schema import SignalMessage


def kalshi_signal_to_message(sig: Signal, node_id: str) -> SignalMessage:
    """Wrap a kalshi_bot Signal in a self-describing SignalMessage for Redis."""
    risk_flags: List[str] = []
    if sig.spread_cents is not None and sig.spread_cents > cfg.MAX_SPREAD_CENTS:
        risk_flags.append("WIDE_SPREAD")
    if sig.confidence > 0.90:
        risk_flags.append("HIGH_CONFIDENCE")
    if sig.arb_partner:
        risk_flags.append("ARB_PARTNER")

    return SignalMessage(
        source_node       = node_id,
        asset_class       = "kalshi",
        strategy          = sig.model_source or "kalshi_unknown",
        category          = getattr(sig, "category", "fomc"),
        ticker            = sig.ticker,
        exchange          = "kalshi",
        side              = sig.side,
        market_price      = sig.market_price,
        fair_value        = sig.fair_value,
        edge              = sig.edge,
        fee_adjusted_edge = sig.fee_adjusted_edge,
        confidence        = sig.confidence,
        suggested_size    = sig.contracts,
        kelly_fraction    = cfg.KELLY_FRACTION,
        risk_flags        = risk_flags,
        spread_cents      = sig.spread_cents,
        book_depth        = getattr(sig, "book_depth", 0),
        meeting           = sig.meeting,
        outcome           = sig.outcome,
        model_source      = sig.model_source,
        arb_partner       = getattr(sig, "arb_partner", None),
        arb_legs          = getattr(sig, "arb_legs", None),
    )


def message_to_kalshi_signal(msg: SignalMessage) -> Signal:
    """Reconstruct a minimal Signal from a SignalMessage for the Executor."""
    return Signal(
        ticker            = msg.ticker,
        title             = msg.ticker,
        category          = msg.category or "fomc",
        side              = msg.side,
        fair_value        = msg.fair_value,
        market_price      = msg.market_price,
        edge              = msg.edge,
        fee_adjusted_edge = msg.fee_adjusted_edge,
        contracts         = msg.suggested_size,
        confidence        = msg.confidence,
        model_source      = msg.model_source or msg.strategy,
        spread_cents      = msg.spread_cents,
        meeting           = msg.meeting or "",
        outcome           = msg.outcome or "",
        arb_partner       = msg.arb_partner,
    )
