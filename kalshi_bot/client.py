"""
client.py — Authenticated HTTP client for the Kalshi REST API v2.

Supports both synchronous and async (concurrent) request patterns.
Concurrent order book fetching reduces full scan time from O(n) sequential
to roughly O(1) wall-clock time bounded by the slowest single request.

Sync usage (simple scripts):
    client = KalshiClient(...)
    data   = client.get("/markets")

Async usage (fast parallel scans):
    results = await client.get_many(["/markets/A/orderbook",
                                     "/markets/B/orderbook"])
"""

import json
import time
import asyncio
import logging

import requests
import httpx

log = logging.getLogger(__name__)

_RETRYABLE = {429, 500, 502, 503, 504}


class KalshiClient:
    """
    Authenticated HTTP client with sync + async support.

    Args:
        base_url:    Full base URL e.g. "https://demo-api.kalshi.co/trade-api/v2"
        auth:        Object with .sign(method, api_path) -> dict
        timeout:     Per-request timeout in seconds
        max_retries: Retry attempts on transient failures
        backoff:     Backoff multiplier (wait = backoff ** attempt seconds)
        concurrency: Max simultaneous async requests
    """

    _API_PREFIX = "/trade-api/v2"

    def __init__(
        self,
        base_url: str,
        auth,
        timeout: int = 10,
        max_retries: int = 3,
        backoff: float = 2.0,
        concurrency: int = 20,
    ):
        self.base_url    = base_url.rstrip("/")
        self.auth        = auth
        self.timeout     = timeout
        self.max_retries = max_retries
        self.backoff     = backoff
        self._concurrency = concurrency
        self._semaphore = None
        self._session    = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def _api_path(self, path: str) -> str:
        return self._API_PREFIX + path

    # ── Sync ──────────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str,
                 params: dict = None, payload: dict = None) -> dict:
        url      = self.base_url + path
        api_path = self._api_path(path)
        body     = json.dumps(payload) if payload else None
        last_exc = None

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                wait = self.backoff ** attempt
                log.warning("Retry %d/%d for %s %s — waiting %.1fs",
                            attempt, self.max_retries, method, path, wait)
                time.sleep(wait)

            try:
                resp = self._session.request(
                    method, url,
                    headers=self.auth.sign(method, api_path),
                    params=params, data=body, timeout=self.timeout,
                )
                if resp.status_code in _RETRYABLE:
                    last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                    continue
                resp.raise_for_status()
                return resp.json()

            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                log.warning("Request error on %s %s (attempt %d): %s",
                            method, path, attempt + 1, exc)

        raise last_exc or RuntimeError(f"All retries exhausted for {method} {path}")

    def get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload=payload)

    # ── Async (concurrent order book fetching) ────────────────────────────────

    async def _async_get(
        self,
        path: str,
        params: dict = None,
        timeout: float | None = None,
    ) -> dict | None:
        """
        Single async GET with semaphore-limited concurrency.
        Returns None on failure so one bad market doesn't abort the batch.

        `timeout` overrides self.timeout for this call (useful for bulk batch
        requests where a shorter per-request timeout prevents the asyncio
        event loop from blocking on hung httpx connection cleanup).
        """
        url      = self.base_url + path
        api_path = self._api_path(path)
        # Use a shorter per-request timeout for async batch fetches so that
        # individual requests fail naturally before asyncio.wait_for needs to
        # cancel them (httpx cleanup after cancellation can block the event loop).
        req_timeout = timeout if timeout is not None else self.timeout

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._concurrency)
        async with self._semaphore:
            for attempt in range(self.max_retries + 1):
                if attempt > 0:
                    await asyncio.sleep(self.backoff ** attempt)
                try:
                    headers = {**self.auth.sign("GET", api_path),
                               "Content-Type": "application/json"}
                    async with httpx.AsyncClient(timeout=req_timeout) as http:
                        resp = await http.get(url, headers=headers, params=params)
                    if resp.status_code in _RETRYABLE:
                        continue
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:
                    log.warning("Async GET failed for %s (attempt %d): %s",
                                path, attempt + 1, exc)
        return None

    async def get_many(
        self,
        paths: list[str],
        per_request_timeout: float | None = None,
    ) -> list[dict | None]:
        """
        Fetch multiple paths concurrently.
        Returns results in the same order as input. None = fetch failed.

        `per_request_timeout` sets a shorter httpx timeout for each request so
        they fail naturally (via ConnectTimeout / ReadTimeout) rather than
        requiring asyncio cancellation to tear them down.  Defaults to
        self.timeout when omitted.

        Example:
            paths   = [f"/markets/{t}/orderbook" for t in tickers]
            results = await client.get_many(paths, per_request_timeout=5.0)
        """
        # Reset semaphore each call — asyncio.run() creates a new event loop each
        # time fetch_signals is called, so the previous semaphore is stale.
        self._semaphore = asyncio.Semaphore(self._concurrency)
        return await asyncio.gather(
            *[self._async_get(p, timeout=per_request_timeout) for p in paths]
        )
