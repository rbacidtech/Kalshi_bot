"""
ep_schema.py — Redis message schema as Python dataclasses.

Full field reference: SCHEMA.md
Every class has:
  .to_redis()    → {"payload": "<json>"}  for XADD
  .from_redis()  → instance               from XREADGROUP mapping
"""

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# ep_config must be imported first so sys.path is set before kalshi_bot imports
from ep_config import SIGNAL_TTL, log


@dataclass
class SignalMessage:
    """
    Self-describing edge signal published by Intel, consumed by Exec.

    All fields needed to make a trade decision are inline.
    Exec never calls back to Intel — it acts on this message alone.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    signal_id:        str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version:   str = "1"
    msg_type:         str = "SIGNAL"
    source_node:      str = ""
    ts_us:            int = field(default_factory=lambda: int(time.time() * 1_000_000))
    ttl_ms:           int = SIGNAL_TTL

    # ── Classification ────────────────────────────────────────────────────────
    asset_class: str = ""  # "kalshi" | "btc_spot" | "cme_btc_basis"
    strategy:    str = ""  # "fomc_directional" | "fomc_arb" | "btc_mr" | ...
    category:    str = ""  # "fomc" | "arb" | "weather" | "mean_reversion" | "basis"

    # ── Market ────────────────────────────────────────────────────────────────
    ticker:   str = ""
    exchange: str = ""    # "kalshi" | "coinbase" | "bybit" | "cme"
    side:     str = ""    # "yes" | "no"  (Kalshi)  "buy" | "sell"  (BTC/CME)

    # ── Pricing ───────────────────────────────────────────────────────────────
    market_price:      float = 0.0   # current mid  (0–1 Kalshi, USD BTC)
    fair_value:        float = 0.0   # model-derived fair value (same units)
    edge:              float = 0.0   # abs(fair_value - market_price)
    fee_adjusted_edge: float = 0.0   # edge after estimated exchange fees

    # ── Confidence & Kelly sizing ─────────────────────────────────────────────
    confidence:     float = 0.0   # [0, 1]
    suggested_size: int   = 1     # contracts / units (Kelly sized on Intel)
    kelly_fraction: float = 0.0   # fraction of bankroll Kelly recommends

    # ── Execution priority (lower = processed first within a batch) ───────────
    # 1 = arb/structural  2 = coherence  3 = directional (default)
    # Intel sets this before publishing; Exec sorts each consumed batch by it.
    priority: int = 3

    # ── Risk flags (advisory — Exec applies its own gates regardless) ─────────
    risk_flags: List[str] = field(default_factory=list)
    # "WIDE_SPREAD" | "LOW_LIQUIDITY" | "STALE_DATA"
    # "HIGH_CONFIDENCE" | "NEAR_EXPIRY" | "MODEL_DIVERGENCE" | "ARB_PARTNER"

    # ── Market microstructure ─────────────────────────────────────────────────
    spread_cents: Optional[int] = None
    book_depth:   Optional[int] = None

    # ── FOMC-specific (null for non-FOMC) ─────────────────────────────────────
    meeting:      Optional[str] = None   # "2025-05"
    outcome:      Optional[str] = None   # "HOLD" | "CUT_25" | "CUT_50" | ...
    model_source: Optional[str] = None   # "fedwatch+zq" | "fred_anchor_3.75%"
    arb_partner:  Optional[str] = None   # paired ticker for arb signals

    # ── Multi-leg arb (null for single-leg and arb_partner-style 2-leg arbs) ──
    # Each element: {"ticker": str, "side": str, "price_cents": int}
    # When set, Exec calls execute_arb_legs() instead of the normal execute() path.
    # arb_partner (above) handles 2-leg monotonicity arbs via its own code path;
    # arb_legs carries butterfly and any other N-leg structural arbs.
    arb_legs: Optional[List[dict]] = None

    # ── BTC mean-reversion (null for non-BTC) ─────────────────────────────────
    btc_price:      Optional[float] = None
    btc_z_score:    Optional[float] = None   # std devs from rolling mean
    btc_lookback_m: Optional[int]   = None   # rolling window in minutes

    # ── Market timing ─────────────────────────────────────────────────────────
    close_time: Optional[str] = None   # RFC3339 market close/expiry time
    # e.g. "2025-05-07T16:00:00Z" — Exec uses this for pre-expiry exits

    # ── CME basis — future-ready (null until implemented) ────────────────────
    futures_ticker: Optional[str]   = None   # "BTCM5"
    basis_bps:      Optional[float] = None   # (futures - spot) / spot * 10_000
    carry_rate:     Optional[float] = None   # annualised carry

    # ── Validation ───────────────────────────────────────────────────────────

    def __post_init__(self) -> None:
        """Validate signal fields on creation."""
        # market_price must be in (0, 1) exclusive — only meaningful for Kalshi
        # probability markets; skip for BTC/CME where market_price is a USD price.
        if self.asset_class in ("kalshi",) or (
            self.asset_class == "" and self.exchange == "kalshi"
        ):
            if not (0.0 < self.market_price < 1.0):
                raise ValueError(
                    f"SignalMessage.market_price={self.market_price} not in (0,1)"
                )
            # fair_value must be in [0, 1] for probability markets
            if not (0.0 <= self.fair_value <= 1.0):
                raise ValueError(
                    f"SignalMessage.fair_value={self.fair_value} not in [0,1]"
                )
        # edge must be non-negative
        if self.edge < 0:
            raise ValueError(f"SignalMessage.edge={self.edge} is negative")
        # side must be a recognised direction token
        if self.side and self.side not in ("yes", "no", "buy", "sell"):
            raise ValueError(f"SignalMessage.side={self.side!r} invalid")
        # confidence in [0, 1]
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"SignalMessage.confidence={self.confidence} not in [0,1]"
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def is_expired(self) -> bool:
        # age in microseconds; ttl_ms is milliseconds → * 1000 to match units.
        # Variable kept as age_us (not renamed) for grep compatibility; math
        # is unchanged.
        age_us = int(time.time() * 1_000_000) - self.ts_us
        return age_us > self.ttl_ms * 1_000

    def to_redis(self) -> Dict[str, str]:
        """Flat string mapping for XADD (Redis Streams requirement)."""
        return {"payload": json.dumps(asdict(self))}

    _SUPPORTED_VERSIONS = frozenset(("1",))

    @classmethod
    def from_redis(cls, mapping: Dict) -> "SignalMessage":
        key  = b"payload" if b"payload" in mapping else "payload"
        data = json.loads(mapping[key])
        # Schema version check — surface mismatches loudly so incompatible
        # message formats don't get silently degraded by field-filtering.
        # Unknown versions are accepted with a warning so we don't block a
        # rollout of a newer Intel / Exec binary; known-bad can be added.
        v = str(data.get("schema_version", "1"))
        if v not in cls._SUPPORTED_VERSIONS:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "SignalMessage schema_version=%s not in supported set %s — "
                "processing best-effort; new fields will be dropped",
                v, sorted(cls._SUPPORTED_VERSIONS),
            )
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class ExecutionReport:
    """
    Published by Exec after every SignalMessage is processed (filled OR rejected).
    Intel reads these to track fills and confirm dedup state is correct.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    exec_id:        str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_id:      str = ""
    schema_version: str = "1"
    msg_type:       str = "EXECUTION_REPORT"
    source_node:    str = ""
    ts_us:          int = field(default_factory=lambda: int(time.time() * 1_000_000))

    # ── Result ────────────────────────────────────────────────────────────────
    status: str = "unknown"
    # "filled" | "rejected" | "expired" | "duplicate" | "failed"

    reject_reason: Optional[str] = None
    # "EXPIRED" | "DUPLICATE" | "BALANCE_UNKNOWN" | "RISK_GATE_SIZE"
    # "RISK_GATE_SPREAD" | "RISK_GATE_EXPOSURE" | "RISK_GATE_DRAWDOWN"
    # "RISK_GATE_KALSHI" | "UNKNOWN_ASSET_CLASS" | "HTTP_ERROR"

    # ── Fill details (set when status == "filled") ────────────────────────────
    ticker:      str   = ""
    asset_class: str   = ""
    side:        str   = ""
    contracts:   int   = 0
    fill_price:  float = 0.0
    order_id:    str   = ""
    mode:        str   = "paper"   # "paper" | "live"

    # ── Cost accounting ───────────────────────────────────────────────────────
    cost_cents:    int   = 0     # fill_price * contracts * 100
    fee_cents:     int   = 0     # estimated Kalshi fee on this trade
    # DEPRECATED 2026-04-29: edge_captured was double-purposed — per-contract
    # decimal on entries, dollar realized PnL on exits. Mixed-unit summing
    # corrupted the advisor's drawdown metric. Kept populated for backward
    # compat; new consumers should read predicted_edge or realized_pnl_cents
    # below based on whether they care about pre-fill expectation or realized
    # outcome.
    edge_captured: float = 0.0
    # Entry-only: per-contract decimal edge net of fees (sig.edge - fee_per_contract).
    # Exit reports leave this at 0.0. The advisor's recent_pnl_edge metric reads
    # only this field, ignoring exits, so single-trade losses can no longer dwarf
    # hundreds of small entry edges.
    predicted_edge:     float = 0.0
    # Exit-only: total realized PnL in cents (move_cents * contracts, fees deducted).
    # Entry reports leave this at 0. Postgres column is nullable so older rows
    # written before the refactor have NULL here and are excluded from new
    # aggregates by IS NOT NULL filters.
    realized_pnl_cents: int   = 0

    # ── Validation ───────────────────────────────────────────────────────────

    # Primary valid statuses (task spec).  "failed" is a legacy alias kept for
    # backwards-compatibility with old stream entries; "unknown" is the dataclass
    # default used when reconstructing partial records via from_redis().
    _VALID_STATUSES = frozenset(
        ("filled", "rejected", "duplicate", "expired", "error", "failed", "unknown")
    )

    def __post_init__(self) -> None:
        """Validate execution report fields on creation."""
        if self.status not in self._VALID_STATUSES:
            raise ValueError(
                f"ExecutionReport.status={self.status!r} must be one of "
                f"{sorted(self._VALID_STATUSES)}"
            )

    def to_redis(self) -> Dict[str, str]:
        return {"payload": json.dumps(asdict(self))}

    @classmethod
    def from_redis(cls, mapping: Dict) -> "ExecutionReport":
        key  = b"payload" if b"payload" in mapping else "payload"
        data = json.loads(mapping[key])
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class PriceSnapshot:
    """
    Intel publishes this every cycle to ep:prices so Exec can run exit
    checks without its own WebSocket connection.

    prices: { ticker: {yes_price, no_price, spread, last_price} }
    """
    msg_type:    str             = "PRICE_SNAPSHOT"
    source_node: str             = ""
    ts_us:       int             = field(default_factory=lambda: int(time.time() * 1_000_000))
    prices:      Dict[str, dict] = field(default_factory=dict)

    def to_redis_hash(self) -> Dict[str, str]:
        """Each ticker becomes a separate field in the ep:prices hash."""
        return {
            ticker: json.dumps({**snap, "ts_us": self.ts_us})
            for ticker, snap in self.prices.items()
        }
