"""
ep_telegram.py — Telegram trade alerts for EdgePulse.

Sends fill confirmations, exit notifications, and system alerts to a
Telegram channel.  All calls are fire-and-forget (errors are logged at
DEBUG so they never crash the main loop).

Configuration (.env):
    TELEGRAM_BOT_TOKEN          — from @BotFather
    TELEGRAM_CHANNEL_ID         — e.g. @mychannel or numeric -100xxxxxxxxxx
    TELEGRAM_ADMIN_ID           — (optional) user ID for CRITICAL alerts
    TELEGRAM_MIN_EDGE_TO_ALERT  — minimum edge to send a fill alert (default 0.0)

Usage:
    from ep_telegram import telegram
    await telegram.send_fill(sig, contracts, mode)
    await telegram.send_exit(ticker, side, contracts, current_cents, reason, pnl_cents)
    await telegram.send_alert("Drawdown limit hit", level="critical")
    await telegram.send_trade_alert(ticker, side, contracts, entry_cents, strategy)
    await telegram.send_circuit_breaker_alert(name, failure_count)
    await telegram.send_daily_summary(pnl_cents, trades, win_rate, open_positions)
"""

import os
import time
from typing import Optional

import httpx

from ep_config import log

_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN",         "")
_CHANNEL    = os.getenv("TELEGRAM_CHANNEL_ID",        "")
_ADMIN      = os.getenv("TELEGRAM_ADMIN_ID",          "")
_MIN_EDGE   = float(os.getenv("TELEGRAM_MIN_EDGE_TO_ALERT", "0.0"))
_API        = "https://api.telegram.org/bot{token}/sendMessage"

_MAX_MESSAGE_LENGTH = 4096
_RATE_LIMIT_SECONDS = 3       # minimum seconds between any two sends
_DEDUP_WINDOW       = 60      # seconds within which identical text is suppressed


class TelegramAlerter:
    """
    Thin async wrapper around the Telegram Bot API.

    All public methods return True on success, False on failure or no-op.
    Errors are caught and logged at DEBUG so they never raise into the caller.

    Rate limiting: at most 1 message per _RATE_LIMIT_SECONDS seconds.
    Deduplication: identical message text sent within _DEDUP_WINDOW seconds
    is silently dropped.
    """

    def __init__(self) -> None:
        self._token    = _TOKEN
        self._channel  = _CHANNEL
        self._admin    = _ADMIN
        self._min_edge = _MIN_EDGE
        self.enabled   = bool(self._token and self._channel)

        self._last_send_ts: float = 0.0               # epoch of last successful send
        self._recent_texts: dict  = {}                # text → epoch of last send

        if self.enabled:
            log.info("Telegram alerts enabled → channel=%s  min_edge=%.2f",
                     self._channel, self._min_edge)
        else:
            log.info("Telegram disabled — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHANNEL_ID to enable")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _truncate(self, text: str) -> str:
        """Truncate message to Telegram's 4096-character limit."""
        if len(text) <= _MAX_MESSAGE_LENGTH:
            return text
        return text[:_MAX_MESSAGE_LENGTH - 3] + "..."

    def _is_duplicate(self, text: str) -> bool:
        """Return True if this exact text was sent within _DEDUP_WINDOW seconds."""
        sent_at = self._recent_texts.get(text)
        if sent_at is None:
            return False
        return (time.time() - sent_at) < _DEDUP_WINDOW

    def _record_sent(self, text: str) -> None:
        """Record that this text was sent; prune stale entries."""
        now = time.time()
        self._last_send_ts = now
        self._recent_texts[text] = now
        # Prune entries older than the dedup window to prevent unbounded growth
        cutoff = now - _DEDUP_WINDOW
        self._recent_texts = {k: v for k, v in self._recent_texts.items() if v >= cutoff}

    async def _send(self, chat_id: str, text: str) -> bool:
        """
        Post a message to chat_id.  Returns True on HTTP 200, False otherwise.
        Applies rate limiting and deduplication.
        """
        if not self.enabled or not chat_id:
            return False

        text = self._truncate(text)

        # Dedup check
        if self._is_duplicate(text):
            log.debug("Telegram dedup: suppressing identical message sent within %ds", _DEDUP_WINDOW)
            return False

        # Rate limit: enforce minimum gap between sends
        elapsed = time.time() - self._last_send_ts
        if elapsed < _RATE_LIMIT_SECONDS:
            import asyncio as _asyncio
            await _asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)

        url = _API.format(token=self._token)
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.post(url, json={
                    "chat_id":    chat_id,
                    "text":       text,
                    "parse_mode": "HTML",
                })
                if r.status_code == 200:
                    self._record_sent(text)
                    return True
                else:
                    log.debug("Telegram API %d: %s", r.status_code, r.text[:200])
                    return False
        except Exception as exc:
            log.debug("Telegram send failed: %s", exc)
            return False

    # ── New public API ────────────────────────────────────────────────────────

    async def send_alert(self, message: str, level: str = "INFO") -> bool:
        """
        Send a system alert.  Returns True on success, False on failure.
        Silently no-ops (returns False) if TELEGRAM_BOT_TOKEN is not configured.

        level: "INFO" | "WARNING" | "CRITICAL"  (case-insensitive)
        CRITICAL alerts also go to TELEGRAM_ADMIN_ID if configured.
        """
        if not self.enabled:
            return False
        try:
            level_lower = level.lower()
            emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level_lower, "ℹ️")
            text  = f"{emoji} <b>EdgePulse</b>: {message}"
            ok    = await self._send(self._channel, text)
            if level_lower == "critical" and self._admin:
                await self._send(self._admin, text)
            return ok
        except Exception as exc:
            log.debug("Telegram send_alert failed: %s", exc)
            return False

    async def send_trade_alert(
        self,
        ticker:      str,
        side:        str,
        contracts:   int,
        entry_cents: int,
        strategy:    str,
    ) -> bool:
        """
        Send a formatted trade execution alert.  Returns True on success.
        Silently no-ops if TELEGRAM_BOT_TOKEN is not configured.
        """
        if not self.enabled:
            return False
        try:
            side_upper = side.upper()
            text = (
                f"🎯 <b>NEW TRADE</b>\n"
                f"📈 {ticker} {side_upper} ×{contracts}\n"
                f"💰 Entry: {entry_cents}¢  Strategy: {strategy}"
            )
            return await self._send(self._channel, text)
        except Exception as exc:
            log.debug("Telegram send_trade_alert failed: %s", exc)
            return False

    async def send_circuit_breaker_alert(self, name: str, failure_count: int) -> bool:
        """
        Send alert when a circuit breaker opens.  Returns True on success.
        Silently no-ops if TELEGRAM_BOT_TOKEN is not configured.
        """
        if not self.enabled:
            return False
        try:
            text = (
                f"⚠️ <b>CIRCUIT BREAKER OPEN</b>\n"
                f"Service: {name}  Failures: {failure_count}"
            )
            return await self._send(self._channel, text)
        except Exception as exc:
            log.debug("Telegram send_circuit_breaker_alert failed: %s", exc)
            return False

    async def send_daily_summary(
        self,
        pnl_cents:      int,
        trades:         int,
        win_rate:       float,
        open_positions: int,
        # Legacy keyword arguments kept for backwards compatibility
        fills:       Optional[int]   = None,
        rejects:     Optional[int]   = None,
        pnl_str:     Optional[str]   = None,
        top_markets: Optional[list]  = None,
    ) -> bool:
        """
        Send daily P&L summary.  Returns True on success.
        Silently no-ops if TELEGRAM_BOT_TOKEN is not configured.

        New signature (positional):
            pnl_cents, trades, win_rate, open_positions

        Legacy signature (keyword, backwards-compatible):
            fills, rejects, pnl_str, top_markets
        """
        if not self.enabled:
            return False
        try:
            # Detect legacy call style (keyword args, no positional pnl_cents)
            if fills is not None or pnl_str is not None:
                # Legacy path
                _fills   = fills   or 0
                _rejects = rejects or 0
                _pnl_str = pnl_str or "$0.00"
                top_str  = "\n".join(
                    f"  {t}  edge={e:.3f}" for t, e in (top_markets or [])[:3]
                ) or "  —"
                text = (
                    f"📊 <b>EdgePulse Daily Summary</b>\n"
                    f"Fills: {_fills}   Rejects: {_rejects}\n"
                    f"Session P&amp;L: {_pnl_str}\n"
                    f"Top markets:\n{top_str}"
                )
            else:
                # New path
                sign     = "+" if pnl_cents >= 0 else ""
                pnl_usd  = pnl_cents / 100.0
                text = (
                    f"📊 <b>Daily Summary</b>\n"
                    f"P&amp;L: {sign}${pnl_usd:.2f}  Trades: {trades}"
                    f"  Win rate: {win_rate:.1f}%\n"
                    f"Open positions: {open_positions}"
                )
            return await self._send(self._channel, text)
        except Exception as exc:
            log.debug("Telegram send_daily_summary failed: %s", exc)
            return False

    # ── Legacy fill/exit helpers (kept for existing ep_exec.py call-sites) ───

    async def send_fill(
        self,
        ticker:      str,
        side:        str,
        contracts:   int,
        price_cents: int,
        mode:        str,
        edge:        float = 0.0,
        strategy:    str   = "",
    ) -> bool:
        """Alert on a new entry fill."""
        if not self.enabled:
            return False
        if edge < self._min_edge:
            return False
        try:
            mode_tag  = "🔴 <b>LIVE</b>" if mode == "live" else "📝 PAPER"
            side_tag  = "✅ YES" if side in ("yes", "buy") else "🔻 NO"
            strat_str = f"  <i>{strategy}</i>" if strategy else ""

            text = (
                f"{mode_tag} FILL\n"
                f"{side_tag}  <b>{ticker}</b>{strat_str}\n"
                f"×{contracts} @ {price_cents}¢   edge={edge:.3f}"
            )
            return await self._send(self._channel, text)
        except Exception as exc:
            log.debug("Telegram send_fill failed: %s", exc)
            return False

    async def send_exit(
        self,
        ticker:        str,
        side:          str,
        contracts:     int,
        current_cents: int,
        reason:        str,
        pnl_cents:     float,
        mode:          str = "paper",
    ) -> bool:
        """Alert on an exit (take-profit, stop-loss, pre-expiry, etc.)."""
        if not self.enabled:
            return False
        try:
            pnl_emoji = "💚" if pnl_cents >= 0 else "🔴"
            mode_tag  = "🔴 <b>LIVE</b>" if mode == "live" else "📝 PAPER"

            text = (
                f"{mode_tag} EXIT\n"
                f"<b>{ticker}</b>  {side.upper()} ×{contracts} @ {current_cents}¢\n"
                f"{pnl_emoji} P&amp;L: {pnl_cents:+.0f}¢\n"
                f"Reason: {reason}"
            )
            return await self._send(self._channel, text)
        except Exception as exc:
            log.debug("Telegram send_exit failed: %s", exc)
            return False


# Module-level singleton — import from anywhere with `from ep_telegram import telegram`
telegram = TelegramAlerter()
