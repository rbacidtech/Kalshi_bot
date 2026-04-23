"""
alerts.py — SMS and email alerts for key bot events.

Triggers:
  - New signal found (edge > threshold)
  - Trade executed (entry or exit)
  - Drawdown limit hit
  - WebSocket disconnected for > 5 minutes
  - Daily summary (configurable time)
  - Source divergence warning

SMS via Twilio (optional — set TWILIO_* env vars to enable).
Email via SMTP (optional — set ALERT_EMAIL_* env vars to enable).

Both are optional and independent. The bot runs fine with neither.
Rate limiting: no more than 1 alert per ticker per 10 minutes to
prevent spam on rapidly-moving markets.
"""

import time
import smtplib
import logging
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import BotState

log = logging.getLogger(__name__)

# Rate limit: minimum seconds between alerts for the same event key
_RATE_LIMIT_SECONDS = 600   # 10 minutes


class AlertManager:
    """
    Subscribes to BotState events and fires SMS/email alerts.

    Args:
        state:           BotState to subscribe to
        twilio_sid:      Twilio Account SID (or None to disable SMS)
        twilio_token:    Twilio Auth Token
        twilio_from:     Twilio phone number (e.g. "+15551234567")
        alert_to_phone:  Your phone number to receive SMS
        smtp_host:       SMTP server hostname (or None to disable email)
        smtp_port:       SMTP port (default 587)
        smtp_user:       SMTP username
        smtp_password:   SMTP password
        alert_from_email: From address
        alert_to_email:  Your email address
        min_edge_cents:  Only alert on signals above this edge (default 10¢)
    """

    def __init__(
        self,
        state: "BotState",
        twilio_sid:       str = None,
        twilio_token:     str = None,
        twilio_from:      str = None,
        alert_to_phone:   str = None,
        smtp_host:        str = None,
        smtp_port:        int = 587,
        smtp_user:        str = None,
        smtp_password:    str = None,
        alert_from_email: str = None,
        alert_to_email:   str = None,
        min_edge_cents:   float = 10.0,
    ):
        self.state            = state
        self.min_edge_cents   = min_edge_cents
        self._rate_limits:    dict[str, float] = {}
        self._lock            = threading.Lock()

        # SMS config
        self._sms_enabled = bool(twilio_sid and twilio_token and twilio_from and alert_to_phone)
        if self._sms_enabled:
            try:
                from twilio.rest import Client
                self._twilio   = Client(twilio_sid, twilio_token)
                self._sms_from = twilio_from
                self._sms_to   = alert_to_phone
                log.info("SMS alerts enabled (to %s).", alert_to_phone)
            except ImportError:
                log.warning("twilio not installed — SMS alerts disabled. "
                            "Run: pip install twilio")
                self._sms_enabled = False

        # Email config
        self._email_enabled = bool(smtp_host and smtp_user and alert_to_email)
        if self._email_enabled:
            self._smtp_host        = smtp_host
            self._smtp_port        = smtp_port
            self._smtp_user        = smtp_user
            self._smtp_password    = smtp_password
            self._email_from       = alert_from_email or smtp_user
            self._email_to         = alert_to_email
            log.info("Email alerts enabled (to %s).", alert_to_email)

        if not self._sms_enabled and not self._email_enabled:
            log.info("No alert channels configured — alerts disabled. "
                     "Set TWILIO_* or ALERT_EMAIL_* in .env to enable.")

        # Subscribe to state events
        state.subscribe(self._on_event)

    # ── Event handler ─────────────────────────────────────────────────────────

    def _on_event(self, event_type: str, data):
        """Route state events to the appropriate alert handler."""
        try:
            if event_type == "trade":
                self._on_trade(data)
            elif event_type == "balance":
                self._on_balance(data)
            elif event_type == "ws_status" and data is False:
                self._on_ws_disconnect()
            elif event_type == "signals":
                self._on_signals(data)
        except Exception as exc:
            log.debug("Alert handler error: %s", exc)

    def _on_trade(self, trade):
        """Alert on every executed trade."""
        rate_key = f"trade:{trade.ticker}:{trade.action}"
        if not self._check_rate(rate_key):
            return

        action_emoji = "🟢" if trade.action == "entry" else "🔴"
        msg = (
            f"{action_emoji} Kalshi {trade.mode.upper()} {trade.action.upper()}\n"
            f"Market: {trade.ticker}\n"
            f"Side: {trade.side.upper()}  Contracts: {trade.contracts}\n"
            f"Price: {trade.price}¢  Edge: {trade.edge:.3f}"
        )
        self._send(f"Trade {trade.action}: {trade.ticker}", msg)

    def _on_balance(self, balance_cents: int):
        """Alert if drawdown limit is approaching."""
        state = self.state
        if state.start_balance_cents <= 0:
            return

        drawdown = 1.0 - (balance_cents / state.start_balance_cents)

        # Warn at 7%, alert at 10%
        if drawdown >= 0.10:
            rate_key = "drawdown:10pct"
            if self._check_rate(rate_key):
                msg = (
                    f"🚨 DRAWDOWN ALERT\n"
                    f"Balance: ${balance_cents/100:.2f}\n"
                    f"Session start: ${state.start_balance_cents/100:.2f}\n"
                    f"Drawdown: {drawdown:.1%}\n"
                    f"Monitor positions closely."
                )
                self._send("DRAWDOWN ALERT", msg)

        elif drawdown >= 0.07:
            rate_key = "drawdown:7pct"
            if self._check_rate(rate_key):
                msg = (
                    f"⚠️ Drawdown Warning\n"
                    f"Current drawdown: {drawdown:.1%} (limit: 10%)\n"
                    f"Balance: ${balance_cents/100:.2f}"
                )
                self._send("Drawdown Warning", msg)

    def _on_ws_disconnect(self):
        """Alert if WebSocket has been disconnected for a while."""
        rate_key = "ws:disconnect"
        if self._check_rate(rate_key):
            msg = (
                "⚠️ WebSocket Disconnected\n"
                "Bot is running on REST polling only.\n"
                "Signal latency increased to ~2 minutes."
            )
            self._send("WebSocket Disconnected", msg)

    def _on_signals(self, signals: list):
        """
        Alert when a strong signal appears.
        Signals are plain dicts (as stored by state.set_signals),
        so use .get() not getattr().
        """
        for signal in signals:
            # Support both dict and object forms defensively
            edge       = signal.get("edge", 0) if isinstance(signal, dict) else getattr(signal, "edge", 0)
            ticker     = signal.get("ticker", "") if isinstance(signal, dict) else getattr(signal, "ticker", "")
            side       = signal.get("side", "") if isinstance(signal, dict) else getattr(signal, "side", "")
            fair_value = signal.get("fair_value", 0) if isinstance(signal, dict) else getattr(signal, "fair_value", 0)
            mkt_price  = signal.get("market_price", 0) if isinstance(signal, dict) else getattr(signal, "market_price", 0)
            confidence = signal.get("confidence", 0) if isinstance(signal, dict) else getattr(signal, "confidence", 0)
            source     = signal.get("model_source", "") if isinstance(signal, dict) else getattr(signal, "model_source", "")

            edge_cents = edge * 100
            if edge_cents < self.min_edge_cents:
                continue

            rate_key = f"signal:{ticker}"
            if not self._check_rate(rate_key):
                continue

            msg = (
                f"📊 FOMC Signal\n"
                f"Market: {ticker}\n"
                f"Side: {side.upper()}\n"
                f"FedWatch: {fair_value:.1%}  "
                f"Kalshi: {mkt_price:.1%}\n"
                f"Edge: {edge_cents:.1f}¢  Conf: {confidence:.0%}\n"
                f"Source: {source}"
            )
            self._send(f"Signal: {ticker}", msg)

    def send_daily_summary(self):
        """Call this once per day to send a session summary."""
        state     = self.state
        snap      = state.snapshot()
        session_pnl = snap["session_pnl"] / 100
        balance     = snap["balance_cents"] / 100
        cycles      = snap["cycle_count"]
        positions   = snap["open_position_count"]

        msg = (
            f"📈 Daily Summary\n"
            f"Mode: {snap['mode'].upper()}\n"
            f"Balance: ${balance:.2f}\n"
            f"Session P&L: ${session_pnl:+.2f}\n"
            f"Cycles run: {cycles}\n"
            f"Open positions: {positions}\n"
            f"WS connected: {'Yes' if snap['ws_connected'] else 'No'}"
        )
        self._send("Daily Summary", msg)

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def _check_rate(self, key: str) -> bool:
        """Return True if enough time has passed since last alert for this key."""
        now = time.monotonic()
        with self._lock:
            last = self._rate_limits.get(key, 0)
            if now - last < _RATE_LIMIT_SECONDS:
                return False
            self._rate_limits[key] = now
            return True

    # ── Delivery ──────────────────────────────────────────────────────────────

    def _send(self, subject: str, body: str):
        """Send alert via all configured channels."""
        if self._sms_enabled:
            self._send_sms(body)
        if self._email_enabled:
            self._send_email(subject, body)

        # Always log alerts regardless of channel config
        log.info("ALERT — %s: %s", subject, body.replace("\n", " | "))

    def _send_sms(self, body: str):
        try:
            self._twilio.messages.create(
                body = body[:1600],   # SMS length limit
                from_= self._sms_from,
                to   = self._sms_to,
            )
            log.debug("SMS sent.")
        except Exception as exc:
            log.warning("SMS failed: %s", exc)

    def _send_email(self, subject: str, body: str):
        import json, urllib.request, os
        api_key = os.getenv("RESEND_API_KEY", "")
        if not api_key:
            return
        try:
            payload = json.dumps({
                "from":    "EdgePulse <onboarding@resend.dev>",
                "to":      [self._email_to],
                "subject": f"[EdgePulse] {subject}",
                "text":    body,
            }).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            log.debug("Email sent to %s.", self._email_to)
        except Exception as exc:
            log.warning("Email failed: %s", exc)
