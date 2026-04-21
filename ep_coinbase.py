"""
ep_coinbase.py — Authenticated Coinbase Advanced Trade client for EdgePulse.

Handles order placement on the Exec node.  The public-only CoinbaseClient
in ep_btc.py handles spot-price reads on the Intel node; this module is
only loaded on the Exec side.

Auth: CDP API Keys (JWT, ES256).
  https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-auth

Env vars:
  COINBASE_API_KEY_NAME    - CDP key name (organizations/.../apiKeys/...)
  COINBASE_PRIVATE_KEY_PATH - path to EC P-256 PEM private key file
  COINBASE_PAPER           - "true" → log orders, never hit the exchange
                             Defaults to same value as KALSHI_PAPER_TRADE.

Notes:
  - base_size is fractional BTC (e.g. "0.0012")
  - Minimum Coinbase order: 0.000016 BTC (~$1 at $62k BTC)
  - IOC market orders; no partial-fill risk management needed here
"""

import base64
import json
import os
import time
import uuid
from typing import Optional

import httpx

from ep_config import log

# ── Config ────────────────────────────────────────────────────────────────────

API_BASE         = "https://api.coinbase.com"
ORDERS_PATH      = "/api/v3/brokerage/orders"
ACCOUNTS_PATH    = "/api/v3/brokerage/accounts"
BTC_MIN_SIZE     = float(os.getenv("COINBASE_BTC_MIN_SIZE", "0.000016"))  # ~$1
BTC_RISK_FRAC    = float(os.getenv("COINBASE_BTC_RISK_FRAC", "0.02"))     # 2% of balance

_KEY_NAME    = os.getenv("COINBASE_API_KEY_NAME", "")
_KEY_PATH    = os.getenv("COINBASE_PRIVATE_KEY_PATH", "")
_paper_env   = os.getenv("COINBASE_PAPER", os.getenv("KALSHI_PAPER_TRADE", "true"))
PAPER        = _paper_env.lower() in ("1", "true", "yes")


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_jwt(key_name: str, private_key_pem: str, method: str, path: str) -> str:
    """
    Generate a short-lived CDP JWT for one API call.

    uri format per Coinbase docs: "METHOD host/path" (no scheme).
    """
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    now    = int(time.time())
    header = {"alg": "ES256", "kid": key_name}
    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} api.coinbase.com{path}",
    }

    h64 = _b64url(json.dumps(header,  separators=(",", ":")).encode())
    p64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h64}.{p64}".encode()

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None
    )
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))

    # DER → fixed-length r||s (64 bytes) for JWT
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return f"{h64}.{p64}.{_b64url(raw_sig)}"


def _load_private_key_pem() -> str:
    """Read PEM from COINBASE_PRIVATE_KEY_PATH or inline env var (\\n-escaped)."""
    if _KEY_PATH and os.path.exists(_KEY_PATH):
        with open(_KEY_PATH) as fh:
            return fh.read()
    # Allow inline PEM with literal \n in the env var
    inline = os.getenv("COINBASE_PRIVATE_KEY_PEM", "")
    if inline:
        return inline.replace("\\n", "\n")
    return ""


# ── Client ────────────────────────────────────────────────────────────────────

class CoinbaseTradeClient:
    """
    Async client for Coinbase Advanced Trade order placement.

    Usage:
        client = CoinbaseTradeClient()
        result = await client.create_market_order("BTC-USD", "BUY", "0.001")
    """

    def __init__(
        self,
        key_name:    str = _KEY_NAME,
        key_path:    str = _KEY_PATH,
        paper:       bool = PAPER,
        timeout:     float = 10.0,
    ):
        self._key_name = key_name
        self._key_path = key_path
        self._paper    = paper
        self._timeout  = timeout
        self._pem:     Optional[str] = None   # lazy-loaded

        if paper:
            log.info("CoinbaseTradeClient: PAPER mode — no real orders will be placed.")
        elif not key_name or not (key_path or os.getenv("COINBASE_PRIVATE_KEY_PEM")):
            log.warning(
                "CoinbaseTradeClient: COINBASE_API_KEY_NAME / COINBASE_PRIVATE_KEY_PATH "
                "not set — falling back to paper mode."
            )
            self._paper = True

    def _get_pem(self) -> str:
        if self._pem is None:
            self._pem = _load_private_key_pem()
        return self._pem

    async def create_market_order(
        self,
        product_id: str,
        side:       str,          # "BUY" or "SELL"
        base_size:  str,          # fractional BTC as string e.g. "0.0012"
    ) -> dict:
        """
        Place an IOC market order.  Returns Coinbase response dict.
        In paper mode returns a synthetic success response.
        """
        client_order_id = str(uuid.uuid4())
        body = {
            "client_order_id": client_order_id,
            "product_id":      product_id,
            "side":            side.upper(),
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": base_size,
                }
            },
        }

        if self._paper:
            log.info(
                "CB PAPER  %s %s base_size=%s  order_id=%s",
                side.upper(), product_id, base_size, client_order_id,
            )
            return {
                "success":        True,
                "order_id":       f"paper-{client_order_id}",
                "client_order_id": client_order_id,
                "paper":          True,
            }

        pem = self._get_pem()
        if not pem:
            log.error("CoinbaseTradeClient: no private key — cannot place order.")
            return {"success": False, "error": "NO_PRIVATE_KEY"}

        try:
            jwt_token = _make_jwt(self._key_name, pem, "POST", ORDERS_PATH)
        except Exception as exc:
            log.error("CoinbaseTradeClient JWT error: %s", exc)
            return {"success": False, "error": f"JWT_ERROR: {exc}"}

        url = f"{API_BASE}{ORDERS_PATH}"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type":  "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                resp = await http.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            log.error("CoinbaseTradeClient request error: %s", exc)
            return {"success": False, "error": f"REQUEST_ERROR: {exc}"}

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code not in (200, 201):
            log.error(
                "CoinbaseTradeClient HTTP %d: %s", resp.status_code, data
            )
            return {"success": False, "error": f"HTTP_{resp.status_code}", "detail": data}

        log.info(
            "CB LIVE  %s %s base_size=%s  order_id=%s",
            side.upper(), product_id, base_size,
            data.get("order_id", "?"),
        )
        return {"success": True, **data}

    async def _fetch_accounts(self) -> Optional[list]:
        """
        Fetch raw Coinbase brokerage accounts list.
        Returns None on any error or if credentials are unavailable.
        """
        pem = self._get_pem()
        if not pem:
            return None
        try:
            jwt_token = _make_jwt(self._key_name, pem, "GET", ACCOUNTS_PATH)
        except Exception as exc:
            log.warning("Coinbase balance JWT error: %s", exc)
            return None
        url = f"{API_BASE}{ACCOUNTS_PATH}"
        headers = {"Authorization": f"Bearer {jwt_token}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                resp = await http.get(url, headers=headers)
        except httpx.RequestError as exc:
            log.warning("Coinbase balance request error: %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("Coinbase balance HTTP %d", resp.status_code)
            return None
        try:
            return resp.json().get("accounts", [])
        except Exception:
            return None

    async def get_usd_balance_cents(self) -> Optional[int]:
        """
        Return available USD cash balance in cents from Coinbase brokerage accounts.
        In paper mode returns None (caller should use paper default).
        Returns None on any error.

        Used for BTC trade sizing — USD cash only, not including BTC holdings.
        For total portfolio value including BTC, use get_total_balance_cents().
        """
        if self._paper:
            return None
        accounts = await self._fetch_accounts()
        if accounts is None:
            return None
        for acct in accounts:
            if acct.get("currency") == "USD":
                avail = acct.get("available_balance", {}).get("value", "0")
                return int(float(avail) * 100)
        return None

    async def get_total_balance_cents(self, btc_price_usd: float = 0.0) -> Optional[int]:
        """
        Return total Coinbase portfolio value in cents (USD cash + BTC holdings).

        btc_price_usd: current BTC/USD spot price for BTC → USD conversion.
                       If 0 or not provided, BTC holdings are excluded.

        Works even in paper mode when API credentials are configured — used
        for balance reporting, not order placement.  Returns None on any error.
        """
        # Allow balance fetch even in paper mode if credentials are configured.
        pem = self._get_pem()
        if not pem:
            return None

        accounts = await self._fetch_accounts()
        if accounts is None:
            return None

        total_cents = 0
        for acct in accounts:
            currency = acct.get("currency", "")
            avail    = float(acct.get("available_balance", {}).get("value", "0") or "0")
            if currency == "USD":
                total_cents += int(avail * 100)
            elif currency == "BTC" and btc_price_usd > 0 and avail > 0:
                total_cents += int(avail * btc_price_usd * 100)

        return total_cents if total_cents > 0 else None


# ── BTC spot price (public, no auth) ─────────────────────────────────────────

async def fetch_btc_spot_usd() -> float:
    """
    Fetch the current BTC-USD spot price from the Coinbase public API.
    Returns 0.0 on any failure so callers can treat it as 'unknown'.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            resp.raise_for_status()
            return float(resp.json()["data"]["amount"])
    except Exception:
        return 0.0


# ── Size helper ───────────────────────────────────────────────────────────────

def btc_base_size(btc_price: float, balance_cents: int) -> str:
    """
    Compute BTC order size string from risk budget.

    Risk = BTC_RISK_FRAC * balance.  Minimum = BTC_MIN_SIZE.
    Returns a string suitable for Coinbase base_size field.
    """
    if btc_price <= 0 or balance_cents <= 0:
        return f"{BTC_MIN_SIZE:.8f}"
    risk_usd  = (balance_cents / 100) * BTC_RISK_FRAC
    size      = max(BTC_MIN_SIZE, risk_usd / btc_price)
    return f"{size:.8f}"
