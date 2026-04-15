"""
executor.py — Trade execution with position exit management.

New in v3: Exit logic.

The original executor only entered positions and never exited.
This version monitors open positions each cycle and sells them when:
  1. The position has moved significantly in your favor (take profit)
  2. The model's fair value has moved against the position (cut loss)
  3. The market is within 24h of resolution (close to avoid surprise risk)

Exit thresholds are configurable. Conservative defaults are set —
let paper trading tell you the right levels before tightening.

CSV log now includes both entry and exit records.
"""

import csv
import json
import logging
import datetime
from datetime import timezone
from pathlib import Path

import requests

from .strategy import Signal

log = logging.getLogger(__name__)

CSV_HEADERS = [
    "timestamp", "ticker", "meeting", "outcome", "side", "action",
    "contracts", "price_cents", "fair_value", "edge",
    "confidence", "model_source", "order_id", "mode",
]


class Executor:
    """
    Executes entry and exit orders in paper or live mode.

    Args:
        client:             KalshiClient
        trades_csv:         Path to CSV log
        paper:              If True, simulate; if False, place real orders
        take_profit_cents:  Exit YES position if price rises by this much
        stop_loss_cents:    Exit if position moves against us by this much
        hours_before_close: Exit all positions this many hours before resolution
    """

    def __init__(
        self,
        client,
        trades_csv: Path,
        paper: bool = True,
        take_profit_cents: int = 20,
        stop_loss_cents:   int = 15,
        hours_before_close: float = 24.0,
        state=None,
    ):
        self.client              = client
        self.trades_csv          = trades_csv
        self.paper               = paper
        self.take_profit_cents   = take_profit_cents
        self.stop_loss_cents     = stop_loss_cents
        self.hours_before_close  = hours_before_close
        self.state               = state   # optional BotState for P&L tracking

        self._held: set[str]                     = set()   # entered this cycle
        self._positions: dict[str, dict]         = {}      # ticker → position info
        self._positions_file = Path(trades_csv).parent / "paper_positions.json"
        self._load_paper_positions()
        self._ensure_csv()

    # ── CSV ───────────────────────────────────────────────────────────────────

    def _ensure_csv(self):
        self.trades_csv.parent.mkdir(parents=True, exist_ok=True)
        if not self.trades_csv.exists():
            with open(self.trades_csv, "w", newline="") as f:
                csv.writer(f).writerow(CSV_HEADERS)
        # Keep file handle open for efficient sequential writes
        self._csv_fh = open(self.trades_csv, "a", newline="", buffering=1)
        self._csv_writer = csv.writer(self._csv_fh)

    def _log_trade(self, signal: Signal, action: str, order_id: str, mode: str):
        row = [
            datetime.datetime.now(timezone.utc).isoformat(),
            signal.ticker,
            signal.meeting,
            signal.outcome,
            signal.side,
            action,          # "entry" or "exit"
            signal.contracts,
            int(signal.market_price * 100),
            signal.fair_value,
            signal.edge,
            signal.confidence,
            signal.model_source,
            order_id,
            mode,
        ]
        self._csv_writer.writerow(row)
        self._csv_fh.flush()

    # ── Cycle management ──────────────────────────────────────────────────────

    def __del__(self):
        """Close CSV file handle on cleanup."""
        try:
            if hasattr(self, '_csv_fh') and self._csv_fh:
                self._csv_fh.close()
        except Exception:
            pass   # intentional: __del__ must not raise

    def reset_cycle(self):
        """Clear per-cycle dedup set. Call at start of each cycle."""
        self._held.clear()

    # ── Entry ─────────────────────────────────────────────────────────────────

    def execute(self, signal: Signal) -> bool:
        """
        Enter a new position. Returns True if executed, False if skipped.
        Skips if already holding this ticker this cycle.
        """
        if signal.ticker in self._held:
            log.debug("Skipping %s — already entered this cycle.", signal.ticker)
            return False
        if signal.ticker in self._positions:
            log.debug("Skipping %s — already in positions.", signal.ticker)
            return False
        # Exposure guard — skip if in-memory positions already exceed $800.
        # (The Redis-based UnifiedRiskEngine applies its own cap before this
        # point; this is a last-resort safety net for the local position cache.)
        total_exposure = sum(
            p.get("entry_cents", 50) * p.get("contracts", 1) / 100
            for p in self._positions.values()
        )
        if total_exposure > 800.00:
            log.debug("Skipping %s — local exposure cap reached ($%.2f).", signal.ticker, total_exposure)
            return False

        if self.paper:
            success = self._paper_entry(signal)
        else:
            success = self._live_entry(signal)

        if success:
            # Track position for exit management
            self._positions[signal.ticker] = {
                "side":          signal.side,
                "entry_cents":   int(signal.market_price * 100),
                "contracts":     signal.contracts,
                "fair_value":    signal.fair_value,
                "meeting":       signal.meeting,
                "outcome":       signal.outcome,
                "entered_at":    datetime.datetime.now(timezone.utc).isoformat(),
            }
            self._save_paper_positions()

        return success

    def _paper_entry(self, signal: Signal) -> bool:
        self._log_trade(signal, action="entry", order_id="paper", mode="paper")
        self._held.add(signal.ticker)
        log.info(
            "[PAPER ENTRY] %-38s  side=%-3s  contracts=%-2d  "
            "price=%d¢  fv=%d¢  edge=%.3f  conf=%.2f  src=%s",
            signal.ticker[:38], signal.side, signal.contracts,
            int(signal.market_price * 100), int(signal.fair_value * 100),
            signal.edge, signal.confidence, signal.model_source,
        )
        return True

    def _live_entry(self, signal: Signal) -> bool:
        price_cents = int(signal.market_price * 100)
        price_key   = "yes_price" if signal.side == "yes" else "no_price"
        payload = {
            "action":  "buy",
            "type":    "limit",
            "ticker":  signal.ticker,
            "side":    signal.side,
            "count":   signal.contracts,
            price_key: price_cents,
        }
        try:
            resp     = self.client.post("/portfolio/orders", payload)
            order_id = resp.get("order", {}).get("order_id", "unknown")
            self._log_trade(signal, "entry", order_id, "live")
            self._held.add(signal.ticker)
            log.info(
                "[LIVE  ENTRY] %-38s  side=%-3s  contracts=%-2d  "
                "price=%d¢  order_id=%s",
                signal.ticker[:38], signal.side, signal.contracts,
                price_cents, order_id,
            )
            return True
        except requests.HTTPError as exc:
            log.error("Entry FAILED for %s: %s", signal.ticker, exc)
            return False

    # ── Exit management ───────────────────────────────────────────────────────


    def _sync_positions_from_kalshi(self):
        """On startup, load open positions from Kalshi so exits work after restarts."""
        try:
            resp = self.client.get("/portfolio/positions")
            positions = resp.get("market_positions", [])
            loaded = 0
            for p in positions:
                ticker = p.get("ticker", "")
                net = p.get("position", 0)
                if net == 0:
                    continue
                side = "yes" if net > 0 else "no"
                contracts = abs(net)
                avg_price = p.get("market_exposure", 0)
                entry_cents = round((avg_price / contracts)) if contracts else 50
                self._positions[ticker] = {
                    "side": side,
                    "contracts": contracts,
                    "entry_cents": entry_cents,
                    "fair_value": 0.5,
                    "meeting": "",
                    "outcome": "",
                }
                loaded += 1
            if loaded:
                log.info("Synced %d open positions from Kalshi on startup", loaded)
        except Exception as e:
            log.warning("Could not sync positions from Kalshi: %s", e)


    def _load_paper_positions(self):
        try:
            if self._positions_file.exists():
                self._positions = json.loads(self._positions_file.read_text())
                if self._positions:
                    log.info("Loaded %d paper positions from disk", len(self._positions))
                    # Sync loaded positions into BotState so dashboard shows them
                    from .state import PositionState
                    import datetime
                    for _ticker, _pos in self._positions.items():
                        try:
                            self.state.open_position(PositionState(
                                ticker      = _ticker,
                                side        = _pos.get("side", "yes"),
                                contracts   = int(_pos.get("contracts", 1)),
                                entry_cents = int(_pos.get("entry_cents", 50)),
                                entry_time  = datetime.datetime.now(datetime.timezone.utc),
                                fair_value  = float(_pos.get("fair_value", 0.5)),
                            ))
                        except Exception as _e:
                            log.debug("State sync skipped %s: %s", _ticker, _e)
            elif not self.paper:
                resp = self.client.get("/portfolio/positions")
                for p in resp.get("market_positions", []):
                    net = p.get("position", 0)
                    if net == 0: continue
                    side = "yes" if net > 0 else "no"
                    contracts = abs(net)
                    exposure = p.get("market_exposure", 0)
                    self._positions[p.get("ticker","")] = {
                        "side": side, "contracts": contracts,
                        "entry_cents": round(exposure/contracts) if contracts else 50,
                        "fair_value": 0.5, "meeting": "", "outcome": "",
                    }
                if self._positions:
                    log.info("Synced %d live positions from Kalshi", len(self._positions))
        except Exception as e:
            log.warning("Could not load positions: %s", e)

    def _save_paper_positions(self):
        try:
            self._positions_file.parent.mkdir(parents=True, exist_ok=True)
            self._positions_file.write_text(json.dumps(self._positions, indent=2))
            log.debug("Saved %d positions to disk", len(self._positions))
        except Exception as e:
            log.warning("Could not save positions: %s", e, exc_info=True)

    def check_exits(self, current_markets: list[dict], current_signals: list[Signal]):
        """
        Review open positions each cycle and exit where warranted.

        Args:
            current_markets:  Fresh market data from scan (for current prices)
            current_signals:  Current model signals (for updated fair values)
        """
        if not self._positions:
            return

        # Build all lookup maps once before the position loop — O(markets) not O(markets*positions)
        price_map      = {
            m["ticker"]: int(float(
                m.get("last_price_dollars") or m.get("yes_bid_dollars")
                or m.get("last_price") or m.get("yes_price") or "0.50"
            ) * 100)
            for m in current_markets
        }
        close_time_map = {
            m["ticker"]: m.get("close_time") or m.get("expiration_time")
            for m in current_markets
        }
        fv_map    = {s.ticker: s for s in current_signals}

        for ticker, pos in list(self._positions.items()):
            current_cents = price_map.get(ticker)
            if current_cents is None:
                continue  # market may have resolved

            entry_cents  = pos["entry_cents"]
            side         = pos["side"]
            contracts    = pos["contracts"]

            # Price movement from our perspective
            if side == "yes":
                move_cents = current_cents - entry_cents
            else:
                move_cents = entry_cents - current_cents

            exit_reason = None

            # Hours-before-close check using pre-built map
            close_time_str = close_time_map.get(ticker)
            if close_time_str:
                try:
                    import datetime as _dt  # already imported at top but safe here
                    close_dt = _dt.datetime.fromisoformat(
                        close_time_str.replace("Z", "+00:00")
                    )
                    hours_remaining = (
                        close_dt - _dt.datetime.now(_dt.timezone.utc)
                    ).total_seconds() / 3600
                    if hours_remaining < self.hours_before_close:
                        exit_reason = (
                            f"approaching resolution "
                            f"({hours_remaining:.1f}h remaining)"
                        )
                except Exception as exc:
                    log.debug('close_time parse error: %s', exc)

            # Take profit
            fv_cents  = int(pos.get("fair_value", 0.80) * 100)
            tp_target = max(self.take_profit_cents, int((fv_cents - entry_cents) * 0.50))
            sl_pct   = 0.50 if entry_cents < 30 else 0.30 if entry_cents < 60 else 0.20
            sl_cents = max(self.stop_loss_cents, int(entry_cents * sl_pct))
            if exit_reason is None and move_cents >= tp_target:
                exit_reason = f"take profit (+{move_cents}¢ of {tp_target}¢ target)"

            elif exit_reason is None and move_cents <= -sl_cents:
                exit_reason = f"stop loss ({move_cents}¢ of -{sl_cents}¢ threshold)"

            # Model reversal: updated fair value now favors the other side
            elif exit_reason is None and ticker in fv_map:
                updated_fv = fv_map[ticker].fair_value
                original_fv = pos["fair_value"]
                if side == "yes" and updated_fv < (original_fv - 0.10):
                    exit_reason = f"model reversal (fv {original_fv:.2f}→{updated_fv:.2f})"
                elif side == "no" and updated_fv > (original_fv + 0.10):
                    exit_reason = f"model reversal (fv {original_fv:.2f}→{updated_fv:.2f})"

            if exit_reason:
                self._exit_position(ticker, pos, current_cents, exit_reason)

    def _exit_position(
        self,
        ticker: str,
        pos: dict,
        current_cents: int,
        reason: str,
    ):
        """Sell an existing position."""
        side      = pos["side"]
        contracts = pos["contracts"]
        # To exit a YES position, sell YES (or equivalently buy NO)
        exit_side = "no" if side == "yes" else "yes"

        # Build a minimal Signal for logging
        exit_signal = Signal(
            ticker            = ticker,
            title             = "",
            category          = pos.get("category", "fomc"),
            meeting           = pos.get("meeting", ""),
            outcome           = pos.get("outcome", ""),
            side              = exit_side,
            fair_value        = pos.get("fair_value", 0.5),
            market_price      = current_cents / 100,
            edge              = 0.0,
            fee_adjusted_edge = 0.0,
            contracts         = contracts,
            confidence        = 0.0,
            model_source      = f"exit: {reason}",
        )

        if self.paper:
            self._log_trade(exit_signal, "exit", "paper", "paper")
            log.info(
                "[PAPER EXIT ] %-38s  side=%-3s  contracts=%-2d  "
                "price=%d¢  reason=%s",
                ticker[:38], exit_side, contracts, current_cents, reason,
            )
        else:
            price_key = "yes_price" if exit_side == "yes" else "no_price"
            payload   = {
                "action":  "buy",
                "type":    "market",     # use market order to ensure fill on exit
                "ticker":  ticker,
                "side":    exit_side,
                "count":   contracts,
                price_key: current_cents,
            }
            try:
                resp     = self.client.post("/portfolio/orders", payload)
                order_id = resp.get("order", {}).get("order_id", "unknown")
                self._log_trade(exit_signal, "exit", order_id, "live")
                log.info(
                    "[LIVE  EXIT ] %-38s  side=%-3s  contracts=%-2d  "
                    "price=%d¢  reason=%s  order_id=%s",
                    ticker[:38], exit_side, contracts, current_cents, reason, order_id,
                )
            except requests.HTTPError as exc:
                log.error("Exit FAILED for %s: %s", ticker, exc)
                return   # don't remove from positions if exit failed

        # Remove from tracked positions and update shared state P&L
        del self._positions[ticker]
        self._save_paper_positions()
        if self.state is not None:
            self.state.close_position(ticker, current_cents)
