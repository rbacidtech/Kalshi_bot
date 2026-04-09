"""
auth.py — Kalshi RSA-PSS request signing.

Loads the private key once and exposes a single function:
    sign(method, path) -> dict of auth headers

Kept separate so it can be unit-tested independently of HTTP logic.
"""

import time
import base64
import logging
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import UnsupportedAlgorithm

log = logging.getLogger(__name__)


class KalshiAuth:
    """
    Loads a PEM private key and signs requests using RSA-PSS.

    Usage:
        auth = KalshiAuth(api_key_id="...", private_key_path=Path("private_key.pem"))
        headers = auth.sign("GET", "/trade-api/v2/markets")
    """

    def __init__(self, api_key_id: str, private_key_path: Path):
        self.api_key_id = api_key_id
        self._private_key = self._load_key(private_key_path)

    @staticmethod
    def _load_key(path: Path):
        """Load and return an RSA private key from a PEM file."""
        with open(path, "rb") as f:
            key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )
        log.debug("Private key loaded from %s", path)
        return key

    def sign(self, method: str, api_path: str) -> dict:
        """
        Return auth headers for a single request.

        Args:
            method:   HTTP verb, e.g. "GET" or "POST"
            api_path: Full API path starting with /trade-api/v2/...

        Returns:
            Dict with KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP,
            and KALSHI-ACCESS-SIGNATURE headers.
        """
        ts_ms   = str(int(time.time() * 1000))
        message = (ts_ms + method.upper() + api_path).encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY":       self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }


class NoAuth:
    """
    Stub auth for paper-trading without credentials.
    Returns empty headers so HTTP calls still work against the demo API.
    """

    def sign(self, method: str, api_path: str) -> dict:
        return {}
