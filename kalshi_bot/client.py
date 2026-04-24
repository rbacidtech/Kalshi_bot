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
import threading

import requests
import httpx

log = logging.getLogger(__name__)

_RETRYABLE = {429, 500, 502, 503, 504}

# Endpoints that are NOT safe to retry on transient errors. A retry here would
# either double-place an order (POST /portfolio/orders — Kalshi may have
# accepted the order before the client saw the timeout) or re-cancel an order
# that was already cancelled under a stale order_id.
# Keep this list conservative — when in doubt, add the path.
_NON_IDEMPOTENT_POST_PATHS = (
    "/portfolio/orders",
)


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
        # Thread-local sessions: `requests.Session` is not thread-safe, but the
        # sync `_request` method is called concurrently via asyncio.to_thread
        # from multiple places (ep_arb, ep_exec, ep_ob_depth). A shared Session
        # causes connection-pool state corruption under load. Give each thread
        # its own Session so the pool is private per caller.
        self._thread_local = threading.local()

    def _api_path(self, path: str) -> str:
        return self._API_PREFIX + path

    def _get_session(self) -> requests.Session:
        """Return this thread's private requests.Session, lazily created."""
        sess = getattr(self._thread_local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"Content-Type": "application/json"})
            self._thread_local.session = sess
        return sess

    # ── Sync ──────────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str,
                 params: dict = None, payload: dict = None) -> dict:
        url      = self.base_url + path
        api_path = self._api_path(path)
        body     = json.dumps(payload) if payload else None
        last_exc = None

        # Non-idempotent POSTs (e.g. /portfolio/orders) must NOT retry on
        # ambiguous failures: a Timeout or ConnectionError after Kalshi
        # received the request would cause a duplicate order on retry, and
        # a 5xx response might also be post-accept. Retry only on clearly
        # idempotent methods / paths.
        is_non_idempotent = (
            method.upper() == "POST"
            and any(path.startswith(p) for p in _NON_IDEMPOTENT_POST_PATHS)
        )
        effective_retries = 0 if is_non_idempotent else self.max_retries
        session = self._get_session()

        for attempt in range(effective_retries + 1):
            if attempt > 0:
                wait = self.backoff ** attempt
                log.warning("Retry %d/%d for %s %s — waiting %.1fs",
                            attempt, effective_retries, method, path, wait)
                time.sleep(wait)

            try:
                resp = session.request(
                    method, url,
                    headers=self.auth.sign(method, api_path),
                    params=params, data=body, timeout=self.timeout,
                )
                if resp.status_code in _RETRYABLE:
                    last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                    if is_non_idempotent:
                        # Surface immediately — no retry would be safe.
                        raise last_exc
                    continue
                resp.raise_for_status()
                return resp.json()

            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                if is_non_idempotent:
                    # Ambiguous — order may or may not have reached Kalshi.
                    # Caller must reconcile via orphan recovery, not retry.
                    log.error(
                        "Non-idempotent %s %s FAILED ambiguously: %s — "
                        "NOT retrying; caller must reconcile",
                        method, path, exc,
                    )
                    raise
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
