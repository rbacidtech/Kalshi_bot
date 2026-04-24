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
import time
from datetime import timezone
from pathlib import Path

import requests

from .strategy import Signal

log = logging.getLogger(__name__)


class ArbRollbackFailed(RuntimeError):
    """
    Raised by execute_arb_legs() when a leg placement fails AND at least one
    earlier leg could not be cancelled (Kalshi API error during unwind).

    The `unrecovered` attribute carries the legs that are open on Kalshi but
    were NOT recorded in Redis — the caller must write them as orphaned positions
    so the exit_checker can close them.
    """
    def __init__(self, message: str, unrecovered: list):
        super().__init__(message)
        self.unrecovered: list = unrecovered   # [(ticker, side, order_id), ...]


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

    def execute(self, signal: Signal) -> str:
        """
        Enter a new position.  Returns the Kalshi order_id on success (truthy),
        "paper" for paper-mode entries, or "" on skip/failure (falsy).
        Callers can use the return value directly in a boolean check.
        """
        if signal.ticker in self._held:
            log.debug("Skipping %s — already entered this cycle.", signal.ticker)
            return ""
        if signal.ticker in self._positions:
            log.debug("Skipping %s — already in positions.", signal.ticker)
            return ""
        # NOTE: the Redis-based UnifiedRiskEngine applies MAX_TOTAL_EXPOSURE
        # against the real balance before reaching this point.  Do not add a
        # redundant hard-dollar cap here — it would incorrectly block entries
        # for live accounts whose balance exceeds the paper-mode default.

        if self.paper:
            order_id = "paper" if self._paper_entry(signal) else ""
        else:
            order_id = self._live_entry(signal)

        if order_id:
            # Track position for exit management
            self._positions[signal.ticker] = {
                "side":          signal.side,
                "entry_cents":   int(signal.market_price * 100),
                "contracts":     signal.contracts,
                "fair_value":    signal.fair_value,
                "meeting":       signal.meeting,
                "outcome":       signal.outcome,
                "entered_at":    datetime.datetime.now(timezone.utc).isoformat(),
                "order_id":      order_id,
            }
            self._save_paper_positions()

        return order_id

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

    def _live_entry(self, signal: Signal) -> str:
        """Place a live limit order. Returns the Kalshi order_id on success, "" on failure."""
        market_price = signal.market_price
        if not (0.01 <= market_price <= 0.99):
            log.error("Price %s out of valid range [0.01, 0.99] — refusing order for %s", market_price, signal.ticker)
            return ""
        price_cents = int(market_price * 100) if signal.side == "yes" else int((1.0 - market_price) * 100)
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
            order_id = resp.get("order", {}).get("order_id")
            if not order_id:
                # HTTP 200 but no order object — API returned an error body
                log.error(
                    "Entry FAILED for %s: no order_id in response — %s",
                    signal.ticker, resp,
                )
                return ""
            self._log_trade(signal, "entry", order_id, "live")
            self._held.add(signal.ticker)
            log.info(
                "[LIVE  ENTRY] %-38s  side=%-3s  contracts=%-2d  "
                "price=%d¢  order_id=%s",
                signal.ticker[:38], signal.side, signal.contracts,
                price_cents, order_id,
            )
            return order_id
        except requests.HTTPError as exc:
            log.error("Entry FAILED for %s: HTTP %s", signal.ticker, exc)
            return ""
        except Exception as exc:
            log.error("Entry FAILED for %s: %s", signal.ticker, exc)
            return ""

    # ── Multi-leg arb execution ───────────────────────────────────────────────

    def execute_arb_legs(
        self,
        legs: list,
        contracts_per_leg: int = 1,
        parent_signal = None,
    ) -> list:
        """
        Place each leg of a structural arb sequentially (flat sizing — no Kelly).

        Args:
            legs: list of {"ticker": str, "side": str, "price_cents": int} dicts
            contracts_per_leg: contracts to trade on each leg (default 1)
            parent_signal: the originating Signal — used to write CSV entry rows
                           so arb legs appear in the trades log (P&L tracking).

        Returns:
            list of order_ids (one per leg, in submission order)

        Raises:
            RuntimeError if a leg fails after previous legs have been placed
            (the already-placed legs are best-effort cancelled before raising).
        """
        placed: list = []   # list of (ticker, side, order_id) for legs placed so far

        log.info(
            "[ARB ENTRY] Starting %d-leg arb  contracts_per_leg=%d  legs=%s",
            len(legs),
            contracts_per_leg,
            [(lg["ticker"], lg["side"], lg["price_cents"]) for lg in legs],
        )

        mode = "paper" if self.paper else "live"

        for i, leg in enumerate(legs):
            ticker      = leg["ticker"]
            side        = leg["side"]
            price_cents = int(leg["price_cents"])

            if self.paper:
                order_id   = self._arb_paper_leg(ticker, side, price_cents, contracts_per_leg, i)
                fill_count = contracts_per_leg
            else:
                order_id = self._arb_live_leg(ticker, side, price_cents, contracts_per_leg, i)
                fill_count = 0
                if order_id:
                    # Wait for confirmed fill before proceeding to the next leg.
                    # An unfilled resting order on leg N means leg N+1 would be naked
                    # exposure, not arb. Abort and roll back if not filled in time.
                    fill_status, fill_count = self._poll_fill_sync(order_id)
                    if fill_status != "filled":
                        log.warning(
                            "[ARB ENTRY] Leg %d/%d fill %s (%s %s) — rolling back %d earlier leg(s)",
                            i + 1, len(legs), fill_status, ticker, side, len(placed),
                        )
                        order_id = ""  # treat as failure → trigger rollback below

            if not order_id:
                # This leg failed or didn't fill.  Best-effort cancel already-placed legs.
                log.warning(
                    "[ARB ENTRY] Leg %d/%d FAILED (%s %s) — attempting to cancel %d earlier leg(s)",
                    i + 1, len(legs), ticker, side, len(placed),
                )
                unrecovered = self._arb_cancel_placed(placed)
                if unrecovered:
                    raise ArbRollbackFailed(
                        f"Arb leg {i + 1}/{len(legs)} failed ({ticker} {side}); "
                        f"{len(unrecovered)}/{len(placed)} earlier leg(s) could not be cancelled",
                        unrecovered,
                    )
                raise RuntimeError(
                    f"Arb leg {i + 1}/{len(legs)} failed ({ticker} {side}) "
                    f"after {len(placed)} leg(s) already placed — clean rollback"
                )

            placed.append((ticker, side, order_id))
            log.info(
                "[ARB ENTRY] Leg %d/%d FILLED  %-38s  side=%-3s  price=%d¢  filled=%d  order_id=%s",
                i + 1, len(legs), ticker[:38], side, price_cents, fill_count, order_id,
            )

            # Write CSV entry for this leg immediately after placement so the
            # position is tracked in P&L even if the process crashes later.
            row = [
                datetime.datetime.now(timezone.utc).isoformat(),
                ticker,
                getattr(parent_signal, "meeting", "") or "",
                getattr(parent_signal, "outcome", "") or "",
                side,
                "entry",
                contracts_per_leg,
                price_cents,
                getattr(parent_signal, "fair_value", 0.5),
                getattr(parent_signal, "edge", 0.0),
                getattr(parent_signal, "confidence", 0.0),
                getattr(parent_signal, "model_source", "arb_leg") or "arb_leg",
                order_id,
                mode,
            ]
            self._csv_writer.writerow(row)
            self._csv_fh.flush()

        order_ids = [oid for _, _, oid in placed]
        log.info(
            "[ARB ENTRY] All %d legs placed  order_ids=%s",
            len(legs), order_ids,
        )
        return order_ids

    def _arb_paper_leg(
        self,
        ticker: str,
        side: str,
        price_cents: int,
        contracts: int,
        leg_index: int,
    ) -> str:
        """Simulate placement of one arb leg in paper mode."""
        log.info(
            "[PAPER ARB LEG %d] %-38s  side=%-3s  price=%d¢  contracts=%d",
            leg_index + 1, ticker[:38], side, price_cents, contracts,
        )
        return "paper"

    def _arb_live_leg(
        self,
        ticker: str,
        side: str,
        price_cents: int,
        contracts: int,
        leg_index: int,
    ) -> str:
        """Place one live arb leg.  Returns order_id on success, "" on failure."""
        if not (1 <= price_cents <= 99):
            log.error(
                "Arb leg %d: price_cents=%d out of valid range [1, 99] "
                "— refusing order for %s %s",
                leg_index + 1, price_cents, ticker, side,
            )
            return ""
        price_key = "yes_price" if side == "yes" else "no_price"
        payload = {
            "action":  "buy",
            "type":    "limit",
            "ticker":  ticker,
            "side":    side,
            "count":   contracts,
            price_key: price_cents,
        }
        try:
            resp     = self.client.post("/portfolio/orders", payload)
            order_id = resp.get("order", {}).get("order_id")
            if not order_id:
                log.error(
                    "Arb leg %d FAILED for %s %s: no order_id in response — %s",
                    leg_index + 1, ticker, side, resp,
                )
                return ""
            log.info(
                "[LIVE ARB LEG %d] %-38s  side=%-3s  price=%d¢  contracts=%d  order_id=%s",
                leg_index + 1, ticker[:38], side, price_cents, contracts, order_id,
            )
            return order_id
        except requests.HTTPError as exc:
            log.error("Arb leg %d FAILED for %s %s: HTTP %s", leg_index + 1, ticker, side, exc)
            return ""
        except Exception as exc:
            log.error("Arb leg %d FAILED for %s %s: %s", leg_index + 1, ticker, side, exc)
            return ""

    def _poll_fill_sync(
        self,
        order_id: str,
        timeout_s: float = 10.0,
        poll_interval_s: float = 0.4,
    ) -> tuple[str, int]:
        """
        Synchronous fill poll for a single arb leg.

        Polls GET /portfolio/orders/{order_id} until the order is filled,
        cancelled, or the timeout expires. If the order times out while still
        resting we cancel it and return "timeout" so the caller can roll back.

        Returns:
            (status, fill_count) where status ∈ {"filled", "cancelled", "timeout"}
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                resp        = self.client.get(f"/portfolio/orders/{order_id}")
                order       = resp.get("order", {})
                status      = order.get("status", "")
                fill_count  = float(order.get("fill_count_fp",   0) or 0)
                total_count = float(order.get("initial_count_fp", 1) or 1)

                if status == "filled" or fill_count >= total_count:
                    return "filled", int(fill_count)
                if status in ("canceled", "cancelled", "expired"):
                    return "cancelled", int(fill_count)
            except Exception as exc:
                log.warning("Order poll error for %s: %s", order_id, exc)
            time.sleep(poll_interval_s)

        # Timeout — cancel the resting order before returning
        log.warning("Leg fill timeout (%ss) — cancelling %s", timeout_s, order_id)
        try:
            self.client._request("DELETE", f"/portfolio/orders/{order_id}")
        except Exception as exc:
            log.error("Cancel of timed-out leg %s failed: %s", order_id, exc)
        return "timeout", 0

    def _arb_cancel_placed(self, placed: list) -> list:
        """
        Best-effort cancel of already-placed arb legs when a subsequent leg fails.

        Returns a list of (ticker, side, order_id) tuples for legs where the
        cancel API call failed.  These legs remain open on Kalshi with no Redis
        record — the caller must write them as orphaned positions.
        """
        failed: list = []
        for ticker, side, order_id in reversed(placed):
            if order_id == "paper":
                log.info(
                    "[ARB UNWIND] Paper leg %s %s — no cancel needed", ticker, side
                )
                continue
            try:
                self.client._request("DELETE", f"/portfolio/orders/{order_id}")
                log.info(
                    "[ARB UNWIND] Cancelled leg %s %s  order_id=%s", ticker, side, order_id
                )
            except Exception as exc:
                log.error(
                    "[ARB UNWIND] Cancel FAILED for %s %s order_id=%s: %s "
                    "— leg is OPEN on Kalshi with no Redis record",
                    ticker, side, order_id, exc,
                )
                failed.append((ticker, side, order_id))
        return failed

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
                    # Sync loaded positions into BotState so dashboard shows them.
                    # Exec runs with state=None (state lives in Redis); skip the
                    # sync entirely in that case rather than logging a NoneType
                    # error per-ticker at every startup.
                    if self.state is not None:
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

            # Arb legs must not be stopped out individually — their hedge is the other leg.
            _is_arb_leg = bool(pos.get("arb_id")) or ("_arb" in (pos.get("model_source") or ""))

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

            # Take profit / stop loss — skipped for arb legs
            fv_cents  = int(pos.get("fair_value", 0.80) * 100)
            tp_target = max(self.take_profit_cents, int((fv_cents - entry_cents) * 0.50))
            sl_pct   = 0.50 if entry_cents < 30 else 0.30 if entry_cents < 60 else 0.20
            sl_cents = max(self.stop_loss_cents, int(entry_cents * sl_pct))
            if exit_reason is None and not _is_arb_leg and move_cents >= tp_target:
                exit_reason = f"take profit (+{move_cents}¢ of {tp_target}¢ target)"

            elif exit_reason is None and not _is_arb_leg and move_cents <= -sl_cents:
                exit_reason = f"stop loss ({move_cents}¢ of -{sl_cents}¢ threshold)"

            # Model reversal — also skipped for arb legs (no model drives them)
            elif exit_reason is None and not _is_arb_leg and ticker in fv_map:
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
    ) -> str:
        """
        Sell an existing position.

        Returns:
            "paper"     — paper-mode simulated exit (always succeeds)
            order_id    — live exit order placed; position marked pending_exit
                          (caller must keep Redis entry and wait for fill poll)
            ""          — exit order failed (position kept, caller retries)
        """
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
            del self._positions[ticker]
            self._save_paper_positions()
            if self.state is not None:
                self.state.close_position(ticker, current_cents)
            return "paper"
        else:
            # Sell the contracts we own (same side as entry).
            # Kalshi has no true market orders — all sells require a limit price.
            # Use the current market price so the order rests at fair value.
            price_field = "yes_price" if side == "yes" else "no_price"
            price_val   = current_cents if side == "yes" else (100 - current_cents)
            payload = {
                "action":    "sell",
                "type":      "limit",
                "ticker":    ticker,
                "side":      side,
                "count":     contracts,
                price_field: price_val,
            }
            try:
                resp     = self.client.post("/portfolio/orders", payload)
                order_id = resp.get("order", {}).get("order_id", "") or ""
                self._log_trade(exit_signal, "exit", order_id, "live")
                log.info(
                    "[LIVE  EXIT ] %-38s  side=%-3s  contracts=%-2d  "
                    "price=%d¢  reason=%s  order_id=%s",
                    ticker[:38], side, contracts, current_cents, reason, order_id,
                )
                # Mark in-memory entry as pending_exit so dedup still blocks
                # re-entry while the exit order rests on the exchange.
                # Caller (exit_checker) keeps the Redis position and polls for fill.
                if ticker in self._positions:
                    self._positions[ticker]["pending_exit"] = True
                return order_id
            except requests.HTTPError as exc:
                body = ""
                try:
                    body = exc.response.text[:300]
                except Exception:
                    pass
                log.error("Exit FAILED for %s: HTTP %s — %s", ticker, exc, body)
                return ""
            except Exception as exc:
                log.error("Exit FAILED for %s: %s", ticker, exc)
                return ""
