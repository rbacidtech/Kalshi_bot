"""
models/fomc.py — Fed Funds rate probability model.

Primary signal: CME FedWatch (authenticated OAuth2, derived from 30-day Fed Funds futures).
Second source:  U.S. Treasury bill term structure (FRED DTB3/DTB6/DTB1YR) — permanently
                replaced ZQ futures in the secondary slot after CME CDN-blocked ZQ in 2024.
Supplementary:  WSJ Fed tracker (public page, next meeting only).

Active data sources and their status:
  - CME FedWatch   authenticated OAuth2 API  ACTIVE — primary source, conf 0.90 solo
  - T-bill term    FRED DTB3/DTB6/DTB1YR     ACTIVE — second source (replaced ZQ), conf 0.72
  - CME SR1 SOFR   authenticated OAuth2 API  ACTIVE — fallback when FedWatch unavailable
  - WSJ tracker    public page scrape         ACTIVE — next-meeting cross-check only
  - FRED FF1/2/3   unauthenticated CSV        ACTIVE — fallback market-implied rates
  - ZQ futures     CME direct quotes          DEAD  — CDN/WAF blocked since mid-2024
  - SOFR SR3 (Yahoo) Yahoo Finance SR3=F      DEAD  — delisted from Yahoo Finance ~2024
  - SOFR SR3 (CME) CME API quotes endpoint   DEAD  — 404 on our api_edgepulse subscription

Multi-source confidence model:
  FedWatch alone (solo):               conf = 0.82
  FedWatch + T-bill term structure:    conf = 0.90  <- normal operating mode
  Proximity boost (within 14 days):    conf up to 0.95 (capped)
  When sources diverge > DIVERGENCE_THRESHOLD: conf capped at 0.75 (ZQ/futures only;
    suppressed for T-bill since T-bills measure average rates, not single-meeting probs)

Meeting awareness:
  Kalshi lists separate contracts for each FOMC meeting date.
  This module derives per-meeting probability distributions from each source
  and fuses them into a single blended estimate with a confidence score.

Staleness detection:
  If FedWatch data is more than 10 minutes old during market hours,
  the confidence score is reduced automatically until fresh data arrives.

Fallback chain (when FedWatch is unavailable):
  1. CME SR1 SOFR futures    — 1-month SOFR, same OAuth2 as FedWatch
  2. FRED FF1/FF2/FF3        — market-implied 30-day Fed Funds futures (CSV)
  3. FRED DFEDTARU + heuristic probs — last resort; conf 0.65, no genuine futures data
"""

import re
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

import os
from dotenv import load_dotenv
load_dotenv()
import httpx

from .cache import get_cache

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_TIMEOUT        = 8.0   # seconds per HTTP request
_TTL_FEDWATCH        = 300   # 5 min — Fed futures drift slowly intraday
_TTL_FUTURES         = 60    # 1 min — raw ZQ futures update continuously
_TTL_WSJ             = 600   # 10 min — WSJ page updates less frequently
_TTL_FRED_FUTURES    = 3600  # 1 hr — FRED FF series updates once per business day
_STALE_MINUTES       = 10    # reduce confidence if data older than this

# Minimum agreement between sources before we treat signal as high-confidence
DIVERGENCE_THRESHOLD = 0.04   # 4 cents — if sources disagree by more, warn

# Upcoming FOMC meeting dates for per-meeting CME API requests.
# The authenticated CME API (fedwatch/v1/forecasts) returns one meeting per
# request when a meetingDt param is supplied; multi-date params trigger WAF 403.
# Update annually when the Fed publishes its calendar.
_FOMC_UPCOMING = [
    "2026-04-29", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-16",
]

# Mapping of rate change outcome labels → basis points
OUTCOME_BPS = {
    "HIKE_50":  +50,
    "HIKE_25":  +25,
    "HOLD":       0,
    "CUT_25":   -25,
    "CUT_50":   -50,
    "CUT_75":   -75,
    "CUT_100": -100,
}

# CME month codes for ZQ (Fed Funds futures) contracts
_CME_MONTH = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}


# Pre-compiled regex patterns (avoids recompilation on every call)
_RE_NORM_SPACES  = re.compile(r"[\s\-]+")
_RE_NON_ALPHA    = re.compile(r"[^A-Z0-9_]")
_RE_DATE_PATTERN = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")
_RE_CPI_PATTERNS = [
    re.compile(r"CPI[^%\d]*?(\d+\.\d+)\s*%"),
]
_RE_WSJ_PATTERNS = [
    (re.compile(r"No Change[^%\d]*?(\d+(?:\.\d+)?)\s*%", re.IGNORECASE), "HOLD"),
    (re.compile(r"Cut 25[^%\d]*?(\d+(?:\.\d+)?)\s*%",    re.IGNORECASE), "CUT_25"),
    (re.compile(r"Cut 50[^%\d]*?(\d+(?:\.\d+)?)\s*%",    re.IGNORECASE), "CUT_50"),
    (re.compile(r"Hike 25[^%\d]*?(\d+(?:\.\d+)?)\s*%",   re.IGNORECASE), "HIKE_25"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*%[^%]*?No Change",    re.IGNORECASE), "HOLD"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*%[^%]*?unchanged",    re.IGNORECASE), "HOLD"),
]

_cache = get_cache()

# ── Module-level macro regime state (set by ep_intel.py each cycle) ──────────
_macro_regime: dict = {}


def set_macro_regime(regime: dict) -> None:
    """Called by ep_intel each cycle with fresh macro indicators.

    Expected keys:
      t10y2y: float       — 10Y-2Y yield spread (negative = inverted = recession signal)
      core_cpi_yoy: float — Core CPI year-over-year (CPILFESL)
      pce_yoy: float      — PCE inflation year-over-year (PCEPI) — Fed's actual target
      icsa: float         — Weekly initial jobless claims (ICSA) — labor pulse
      t5yifr: float       — 5Y5Y inflation forward — long-run inflation anchor
      vix: float          — CBOE VIX
      yield_curve_spread: float  — same as t10y2y (alias)
      move_index: float          — ICE BofA MOVE Index (bond market volatility)
      credit_spread_hyg_lqd: float — HYG/LQD ratio (credit spread proxy; falling = risk-off)
    """
    global _macro_regime
    # Validate each field — log warning and skip if out of range
    validated = {}
    _REGIME_RANGES = {
        "t10y2y":          (-5.0, 5.0),
        "core_cpi_yoy":    (0.0, 20.0),
        "pce_yoy":         (0.0, 20.0),
        "icsa":            (100_000, 2_000_000),
        "t5yifr":          (0.0, 10.0),
        "vix":             (5.0, 150.0),
        "yield_curve_spread": (-5.0, 5.0),
        "move_index":         (0.0, 500.0),
        "credit_spread_hyg_lqd": (0.3, 2.0),
    }
    for key, (lo, hi) in _REGIME_RANGES.items():
        val = regime.get(key)
        if val is not None:
            if lo <= val <= hi:
                validated[key] = val
            else:
                log.warning(
                    "set_macro_regime: %s=%.4f out of range [%.1f, %.1f] — ignored",
                    key, val, lo, hi,
                )
    _macro_regime = validated


# Shared async HTTP client — reuses connections across requests (faster than
# creating a new client per request which opens a fresh TCP connection each time)
_http_client: "httpx.AsyncClient | None" = None

async def _get_http_client() -> "httpx.AsyncClient":
    """Return the shared httpx client, creating it if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout  = _TIMEOUT,
            headers  = {"User-Agent": "Mozilla/5.0 (compatible; kalshi-bot/3.0)"},
            follow_redirects = True,
            limits   = httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client




# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class MeetingProbs:
    """
    Probability distribution over rate outcomes for one FOMC meeting.

    probs:        dict of outcome label → probability, sums to ~1.0
                  e.g. {"HOLD": 0.72, "CUT_25": 0.24, "CUT_50": 0.04}
    fetched_at:   UTC timestamp of when this data was retrieved
    sources:      which data sources contributed
    confidence:   0-1 score reflecting source agreement and freshness
    data_quality: "ok" under normal conditions; "fallback_only" when running on
                  FRED static anchor with no CME / ZQ / SR1 / SR3 futures data.
                  Logged by downstream callers to assist in diagnosing bad signals.
    """
    probs:        dict[str, float]
    fetched_at:   datetime
    sources:      list[str]
    confidence:   float = 0.90
    data_quality: str   = "ok"

    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()

    def is_stale(self) -> bool:
        return self.age_seconds() > _STALE_MINUTES * 60

    def get(self, outcome: str) -> float | None:
        """Return probability for a specific outcome, trying multiple key formats."""
        outcome = outcome.upper()
        for key in [outcome, outcome.replace("_", ""), outcome.replace("_", "-")]:
            if key in self.probs:
                return self.probs[key]
        return None


# ── Probability validation helper ─────────────────────────────────────────────

def _validate_probs(probs: dict, source: str, context: str = "") -> bool:
    """Validate probability distribution. Returns True if valid."""
    if not probs:
        log.warning("Probs validation: empty distribution from %s %s", source, context)
        return False
    total = sum(probs.values())
    if not (0.95 <= total <= 1.05):
        log.warning(
            "Probs validation: sum=%.4f (expected ~1.0) from %s %s",
            total, source, context,
        )
        return False
    for k, v in probs.items():
        if not (0.0 <= v <= 1.0):
            log.warning(
                "Probs validation: %s=%.4f out of [0,1] from %s %s",
                k, v, source, context,
            )
            return False
    # Check all keys are known outcomes
    unknown = set(probs) - set(OUTCOME_BPS) - {"HOLD"}
    if unknown:
        log.warning(
            "Probs validation: unknown outcomes %s from %s %s",
            unknown, source, context,
        )
        return False
    return True


# ── Macro regime post-processor ───────────────────────────────────────────────

def _apply_macro_regime_adjustment(probs: dict, meeting_key: str) -> dict:
    """
    Bias probability distribution based on macro regime indicators.
    Returns adjusted probs (still sum to 1.0, each in [0,1]).

    Regime rules (additive basis point adjustments to log-odds, then renormalize):

    Easing pressure (higher CUT probability):
      - Yield curve inverted (t10y2y < -0.25): +8% weight on CUT outcomes
      - PCE < 2.0%: +5% weight on CUT outcomes
      - ICSA > 300k: +4% weight on CUT outcomes (labor softening)
      - VIX > 30: +3% weight on CUT outcomes (risk-off = rate cut pressure)

    Tightening pressure (higher HOLD/HIKE probability):
      - PCE > 2.8%: +6% weight on HOLD/HIKE outcomes
      - Core CPI > 3.0%: +4% weight on HOLD/HIKE outcomes
      - T5YIFR > 2.5%: +3% weight on HOLD/HIKE outcomes (inflation unanchored)
      - Yield curve steep (t10y2y > 1.5): +2% weight on HOLD/HIKE outcomes

    Implementation: use log-odds adjustment to maintain proper probability simplex.
    """
    if not _macro_regime or not probs:
        return probs

    # Categorize outcomes
    cut_outcomes  = {k for k in probs if k.startswith("CUT")}
    hold_outcomes = {k for k in probs if k == "HOLD"}
    hike_outcomes = {k for k in probs if k.startswith("HIKE")}

    easing_mult     = 1.0
    tightening_mult = 1.0

    t10y2y      = _macro_regime.get("t10y2y")
    pce         = _macro_regime.get("pce_yoy")
    core_cpi    = _macro_regime.get("core_cpi_yoy")
    icsa        = _macro_regime.get("icsa")
    t5yifr      = _macro_regime.get("t5yifr")
    vix         = _macro_regime.get("vix")
    move_index  = _macro_regime.get("move_index")
    credit_spread_hyg_lqd = _macro_regime.get("credit_spread_hyg_lqd")

    # Easing signals
    if t10y2y is not None and t10y2y < -0.25:
        easing_mult *= 1.08
    if pce is not None and pce < 2.0:
        easing_mult *= 1.05
    if icsa is not None and icsa > 300_000:
        easing_mult *= 1.04
    if vix is not None and vix > 30:
        easing_mult *= 1.03

    # Tightening signals
    if pce is not None and pce > 2.8:
        tightening_mult *= 1.06
    if core_cpi is not None and core_cpi > 3.0:
        tightening_mult *= 1.04
    if t5yifr is not None and t5yifr > 2.5:
        tightening_mult *= 1.03
    if t10y2y is not None and t10y2y > 1.5:
        tightening_mult *= 1.02

    # Apply multipliers
    adjusted = {}
    for outcome, p in probs.items():
        if outcome in cut_outcomes:
            adjusted[outcome] = p * easing_mult
        elif outcome in hike_outcomes:
            adjusted[outcome] = p * tightening_mult
        else:  # HOLD
            # HOLD gets weighted toward whichever regime is stronger
            if easing_mult > tightening_mult:
                adjusted[outcome] = p  # cuts coming — HOLD decreases
            else:
                adjusted[outcome] = p * tightening_mult  # hawkish — HOLD increases

    # Additive HOLD boosts from new macro signals (applied before renormalization)
    # High bond vol (MOVE > 100) → Fed likely stays put
    if move_index is not None and move_index > 100:
        for outcome in hold_outcomes:
            adjusted[outcome] = adjusted.get(outcome, probs.get(outcome, 0.0)) + 0.02
    # Credit stress (HYG/LQD < 0.72) → widening spreads signal risk-off, Fed pauses
    if credit_spread_hyg_lqd is not None and credit_spread_hyg_lqd < 0.72:
        for outcome in hold_outcomes:
            adjusted[outcome] = adjusted.get(outcome, probs.get(outcome, 0.0)) + 0.03

    # Renormalize to sum=1.0
    total = sum(adjusted.values())
    if total <= 0:
        log.warning(
            "Macro regime adjustment produced zero-sum probs for %s — reverting",
            meeting_key,
        )
        return probs

    normalized = {k: max(0.0, min(1.0, v / total)) for k, v in adjusted.items()}

    # Sanity check: sum must be in [0.99, 1.01]
    check_sum = sum(normalized.values())
    if not (0.99 <= check_sum <= 1.01):
        log.warning(
            "Macro regime normalization sum=%.4f for %s — reverting",
            check_sum, meeting_key,
        )
        return probs

    # Log the adjustment if meaningful (> 1% shift on any outcome)
    max_shift = max(
        abs(normalized.get(k, 0) - probs.get(k, 0))
        for k in set(normalized) | set(probs)
    )
    if max_shift > 0.01:
        log.debug(
            "Macro regime adjusted %s: easing_mult=%.3f tightening_mult=%.3f max_shift=%.3f",
            meeting_key, easing_mult, tightening_mult, max_shift,
        )

    return normalized


# ── Source 1: CME FedWatch ────────────────────────────────────────────────────

def _normalize_cme_api_response(api_data: dict) -> dict | None:
    """
    Convert the authenticated CME FedWatch API v1 response to the internal
    'meetings' format used by fetch_fedwatch_all_meetings().

    CME API returns:
      {"payload": [{"meetingDt": "2026-04-29", "rateRange":
                    [{"lowerRt": 350, "upperRt": 375, "probability": 0.97931}, ...]}]}

    Output format (compatible with _parse_fedwatch_meeting):
      {"meetings": [{"meetingDate": "2026-04-29",
                     "probabilities": {"HOLD": 97.931, "HIKE_25": 2.069}}]}

    Probabilities are stored as 0–100 (percentage) so _parse_fedwatch_meeting()
    correctly divides by 100 to recover [0, 1] floats.

    Rate buckets are 25-bps wide.  Each bucket's upperRt (in bps) minus the
    current rate upper-bound gives the exact change_bps, which maps 1-to-1
    to an OUTCOME_BPS label.  Buckets outside OUTCOME_BPS range are skipped.
    """
    payload = api_data.get("payload", [])
    if not payload:
        return None

    current_bps = round(_current_fed_rate * 100)          # e.g. 3.75 → 375
    _bps_to_outcome = {bps: label for label, bps in OUTCOME_BPS.items()}

    meetings = []
    for item in payload:
        meeting_dt = item.get("meetingDt", "")
        if not meeting_dt:
            continue

        probs: dict[str, float] = {}
        for bucket in item.get("rateRange", []):
            prob = bucket.get("probability")
            if prob is None or prob <= 0:
                continue
            upper_rt = bucket.get("upperRt")
            if upper_rt is None:
                continue
            change_bps = upper_rt - current_bps
            outcome = _bps_to_outcome.get(change_bps)
            if outcome is None:
                continue   # outside OUTCOME_BPS space — skip
            probs[outcome] = probs.get(outcome, 0.0) + prob * 100.0  # → percentage

        if probs:
            # probs here are still percentage-scale (0-100); convert for validation
            _probs_01 = {k: v / 100.0 for k, v in probs.items()}
            if not _validate_probs(_probs_01, "cme_api", f"meeting={meeting_dt}"):
                log.debug(
                    "_normalize_cme_api_response: invalid probs for %s — skipping",
                    meeting_dt,
                )
                continue
            meetings.append({"meetingDate": meeting_dt, "probabilities": probs})

    return {"meetings": meetings} if meetings else None


async def _fetch_fedwatch_raw() -> dict | None:
    """
    Fetch the raw CME FedWatch JSON for all upcoming meetings.
    Returns the full API response dict or None.

    CME has progressively tightened WAF/CDN rules since mid-2024.
    We try multiple known JSON endpoints in order; all return the same
    probability schema so the same parser handles every response.

    NOTE: The HTML page (cme-fedwatch-tool.html) is intentionally NOT
    included here — it does not expose a parseable JSON payload and
    calling resp.json() on it always raises a decode error.
    """
    cache_key = "fedwatch:raw"
    cached    = _cache.get(cache_key)
    if cached:
        return cached

    # ── Attempt 0: Authenticated CME API (OAuth2 client credentials) ────────────
    # Flow: POST auth.cmegroup.com → CME JWT → use JWT directly as Bearer token.
    # NOTE: GCP STS exchange is NOT required — the CME JWT is accepted directly
    #       by the data API, confirmed 2026-04-16.
    # Confirmed data endpoint (2026-04-16):
    #   GET https://markets.api.cmegroup.com/fedwatch/v1/forecasts
    #   Query params: meetingDt (YYYY-MM-DD, repeatable), reportingDt (YYYY-MM-DD, repeatable), limit (int)
    #   Response: {payload:[{meetingDt, rateRange:[{lowerRt (bps), upperRt (bps), probability}]}]}
    #   Required headers: Authorization: Bearer {cme_jwt}, CME-Application-Name, CME-Application-Vendor,
    #                     CME-Application-Version, CME-Request-ID, User-Agent
    # Docs: https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/457320466
    # Intraday variant: https://markets.api.cmegroup.com/fedwatch_rt/v1/forecasts/latest
    _cme_data_url  = os.getenv("CME_FEDWATCH_DATA_URL", "").strip()
    _cme_auth_url  = os.getenv("CME_FEDWATCH_AUTH_URL",
                                "https://auth.cmegroup.com/as/token.oauth2").strip()
    _cme_key_name  = os.getenv("CME_FEDWATCH_API_KEY_NAME", "").strip()
    _cme_password  = os.getenv("CME_FEDWATCH_API_PASSWORD", "").strip()
    if _cme_data_url and _cme_key_name and _cme_password:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as http:
                # Step 1: CME OAuth2 client credentials → CME JWT
                tok_resp = await http.post(
                    _cme_auth_url,
                    data={
                        "grant_type":    "client_credentials",
                        "client_id":     _cme_key_name,
                        "client_secret": _cme_password,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                cme_jwt = tok_resp.json().get("access_token", "")
                if not cme_jwt:
                    raise ValueError(f"CME token step failed: {tok_resp.status_code}")

                # Step 2: Fetch each upcoming FOMC meeting individually and combine.
                # The API only returns one meeting per request; multi-date params
                # trigger WAF 403. Requests are issued concurrently to minimise latency.
                cme_headers = {
                    "Authorization":           f"Bearer {cme_jwt}",
                    "Accept":                  "application/json",
                    "CME-Application-Name":    "EdgePulse",
                    "CME-Application-Vendor":  "EdgePulse",
                    "CME-Application-Version": "1.0",
                    "User-Agent":              "EdgePulse/1.0",
                }
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                # Use live calendar if available; fall back to hardcoded list
                _cal_src    = _FOMC_UPCOMING_LIVE if _FOMC_UPCOMING_LIVE else _FOMC_UPCOMING
                fetch_dates = [d for d in _cal_src if d >= today_str]

                async def _fetch_meeting(date: str, idx: int) -> list:
                    try:
                        r = await http.get(
                            _cme_data_url,
                            params={"meetingDt": date},
                            headers={**cme_headers,
                                     "CME-Request-ID": f"ep-fw-{int(time.time())}-{idx}"},
                        )
                        return r.json().get("payload", []) if r.status_code == 200 else []
                    except Exception:
                        return []

                results = await asyncio.gather(
                    *[_fetch_meeting(d, i) for i, d in enumerate(fetch_dates)]
                )
                combined_payload = [item for sub in results for item in sub]

                if combined_payload:
                    data = _normalize_cme_api_response({"payload": combined_payload})
                    if data:
                        # ── Real-time endpoint: intraday update for next meeting ───
                        # GET /fedwatch_rt/v1/forecasts/latest returns the same schema
                        # as the EOD endpoint but refreshes throughout the trading day.
                        # We use it to override the NEXT meeting's probabilities only
                        # (most liquid contract; intraday moves matter most there).
                        # Cache TTL = 60 s (vs 300 s for EOD).  On 403/404 we silently
                        # continue with EOD data — do NOT break existing functionality.
                        _rt_url = "https://markets.api.cmegroup.com/fedwatch_rt/v1/forecasts/latest"
                        try:
                            rt_resp = await http.get(
                                _rt_url,
                                params={"meetingDt": fetch_dates[0]} if fetch_dates else {},
                                headers={**cme_headers,
                                         "CME-Request-ID": f"ep-fw-rt-{int(time.time())}"},
                            )
                            if rt_resp.status_code == 200:
                                rt_payload = rt_resp.json().get("payload", [])
                                rt_data    = _normalize_cme_api_response({"payload": rt_payload}) if rt_payload else None
                                if rt_data and rt_data.get("meetings"):
                                    # Merge: replace first meeting in EOD data with RT data
                                    rt_meetings = {
                                        m["meetingDate"]: m
                                        for m in rt_data.get("meetings", [])
                                    }
                                    merged = []
                                    for m in data.get("meetings", []):
                                        md = m.get("meetingDate", "")
                                        if md in rt_meetings:
                                            merged.append(rt_meetings[md])
                                            log.info(
                                                "FedWatch RT: overriding EOD probs for next "
                                                "meeting %s with intraday data", md,
                                            )
                                        else:
                                            merged.append(m)
                                    data = {"meetings": merged}
                            elif rt_resp.status_code in (403, 404):
                                log.debug(
                                    "FedWatch RT endpoint returned %d — using EOD data",
                                    rt_resp.status_code,
                                )
                            else:
                                log.debug(
                                    "FedWatch RT endpoint: unexpected status %d",
                                    rt_resp.status_code,
                                )
                        except Exception as rt_exc:
                            log.debug("FedWatch RT fetch failed (non-fatal): %s", rt_exc)

                        # Cache with standard EOD TTL (RT data was already merged in)
                        _cache.set(cache_key, data, ttl=_TTL_FEDWATCH)
                        log.info(
                            "FedWatch fetched via authenticated CME API — %d/%d meetings",
                            len(data.get("meetings", [])), len(fetch_dates),
                        )
                        try:
                            from ep_health import health as _health
                            _health.mark_ok("cme_fedwatch")
                        except ImportError:
                            pass
                        return data
                    log.warning("CME authenticated API: payload received but no parseable meetings")
                else:
                    log.warning("CME authenticated API: all %d meeting requests returned empty",
                                len(fetch_dates))
        except Exception as exc:
            log.warning("CME authenticated fetch failed: %s — falling back to public URLs", exc)

    # Browser-like headers that satisfy CME's WAF checks.
    # All three paths return the same JSON schema when they work.
    _browser_ua_win = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    _browser_ua_mac = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    _referer = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

    attempts = [
        # ── Attempt 1: primary CmeWS MVC endpoint (XHR) ──────────────────────
        (
            "https://www.cmegroup.com/CmeWS/mvc/MarketData/getFedWatch"
            "?selectedDate=&monthlyInterval=1",
            {
                "User-Agent":       _browser_ua_win,
                "Accept":           "application/json, text/javascript, */*; q=0.01",
                "Accept-Language":  "en-US,en;q=0.9",
                "Referer":          _referer,
                "X-Requested-With": "XMLHttpRequest",
            },
        ),
        # ── Attempt 2: alternate path used by newer CME front-end ─────────────
        # Observed serving identical JSON to the MVC endpoint in 2025.
        (
            "https://www.cmegroup.com/CmeWS/mvc/MarketData/getFedWatch"
            "?selectedDate=&monthlyInterval=3",
            {
                "User-Agent":       _browser_ua_mac,
                "Accept":           "application/json, */*; q=0.01",
                "Accept-Language":  "en-US,en;q=0.9",
                "Referer":          _referer,
                "X-Requested-With": "XMLHttpRequest",
                "sec-fetch-dest":   "empty",
                "sec-fetch-mode":   "cors",
                "sec-fetch-site":   "same-origin",
            },
        ),
        # ── Attempt 3: public REST gateway sometimes bypasses CDN rules ───────
        (
            "https://www.cmegroup.com/CmeWS/mvc/MarketData/getFedWatch"
            "?selectedDate=&monthlyInterval=1",
            {
                "User-Agent":      _browser_ua_win,
                "Accept":          "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer":         _referer,
                "Cache-Control":   "no-cache",
                "Pragma":          "no-cache",
            },
        ),
    ]

    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True,
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    ) as http:
        for url, headers in attempts:
            try:
                resp = await http.get(url, headers=headers)
                if resp.status_code in (403, 429, 503):
                    log.debug(
                        "FedWatch %d at %s — CDN/rate-limit block",
                        resp.status_code, url[:80],
                    )
                    continue
                resp.raise_for_status()
                # Verify we actually got JSON before caching
                content_type = resp.headers.get("content-type", "")
                if "html" in content_type:
                    log.debug(
                        "FedWatch returned HTML (not JSON) for %s — skipping",
                        url[:80],
                    )
                    continue
                data = resp.json()
                _cache.set(cache_key, data, ttl=_TTL_FEDWATCH)
                log.debug("FedWatch raw data fetched from %s", url[:80])
                return data
            except Exception as exc:
                log.debug("FedWatch fetch failed for %s: %s", url[:80], exc)

    # CME is blocking all attempts — this is expected and handled gracefully.
    # cme_fedwatch is marked "optional" in ep_health; FRED + Kalshi fill the gap.
    log.info("All FedWatch URLs failed — CME CDN block in effect (expected).")
    try:
        from ep_health import health as _health
        _health.mark_fail("cme_fedwatch", "all URLs returned 403/429 or error")
    except ImportError:
        pass
    return None


def _parse_fedwatch_meeting(meeting_data: dict) -> dict[str, float] | None:
    """
    Parse a single meeting's probability data from FedWatch JSON.

    CME returns probabilities as percentages (0-100), keyed by outcome label.
    We normalise to [0, 1] and standardise key names.
    """
    try:
        raw_probs = meeting_data.get("probabilities") or meeting_data.get("probs", {})
        if not raw_probs:
            return None

        result = {}
        for key, val in raw_probs.items():
            # Normalise key: "Cut 25" → "CUT_25", "Hold" → "HOLD"
            norm_key = _RE_NORM_SPACES.sub("_", str(key).upper().strip())
            norm_key = _RE_NON_ALPHA.sub("", norm_key)
            result[norm_key] = float(val) / 100

        # Ensure probabilities sum close to 1.0
        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}

        if result and not _validate_probs(result, "fedwatch_parse"):
            log.debug("_parse_fedwatch_meeting: invalid probs — returning None")
            return None
        return result if result else None

    except Exception as exc:
        log.debug("FedWatch meeting parse error: %s", exc)
        return None


async def fetch_fedwatch_all_meetings() -> dict[str, MeetingProbs]:
    """
    Fetch and parse FedWatch data for ALL upcoming FOMC meetings.

    Returns dict keyed by meeting date string "YYYY-MM" → MeetingProbs.
    This lets us price contracts for any upcoming meeting, not just the next one.
    """
    data = await _fetch_fedwatch_raw()
    if not data:
        # Fallback chain: CME FedWatch → SR1 SOFR → SR3 SOFR → FRED EFFR → static heuristic
        log.info("CME FedWatch unavailable — trying SR1 SOFR futures.")
        sr1_meetings = await _fetch_sofr_sr1_meetings()
        if sr1_meetings:
            log.info(
                "SR1 SOFR futures: %d meeting months available — using as CME substitute.",
                len(sr1_meetings),
            )
            return sr1_meetings
        log.info("SR1 SOFR unavailable — trying SOFR SR3 futures (CME API).")
        sr3_meetings = await _fetch_sofr_sr3_meetings()
        if sr3_meetings:
            log.info(
                "SOFR SR3 futures: %d meeting months available — using as fallback.",
                len(sr3_meetings),
            )
            return sr3_meetings
        log.info("SOFR SR3 unavailable — trying FRED 30-day Fed Funds Futures (FF1/FF2/FF3).")
        fred_futures = await _fetch_fred_futures_meetings()
        if fred_futures:
            log.info(
                "FRED FF futures: %d meeting months available — using as CME substitute.",
                len(fred_futures),
            )
            return fred_futures
        log.info("FRED FF futures unavailable — falling back to FRED static heuristic model.")
        return await _fetch_fred_fallback_meetings()

    meetings_raw = data.get("meetings") or data.get("data", [])
    if not meetings_raw:
        return {}

    result = {}
    now    = datetime.now(timezone.utc)

    for meeting in meetings_raw:
        try:
            # Parse meeting date
            date_str = (
                meeting.get("meetingDate")
                or meeting.get("date")
                or meeting.get("month", "")
            )
            if not date_str:
                continue

            # Try various date formats CME uses
            meeting_dt = None
            date_str_stripped = str(date_str).strip()
            for fmt in ("%Y-%m-%d", "%Y-%m", "%b %Y", "%B %Y"):
                # Limit to 10 chars to strip trailing timestamps (e.g. "2025-04-14T00:00:00Z")
                # Python slices never error on short strings — short dates are passed through intact
                snippet = date_str_stripped[:10]
                try:
                    meeting_dt = datetime.strptime(snippet, fmt).replace(
                        tzinfo=timezone.utc
                    )
                    break
                except ValueError:
                    continue

            if meeting_dt is None or meeting_dt < now - timedelta(days=1):
                continue  # skip past meetings

            probs = _parse_fedwatch_meeting(meeting)
            if not probs:
                continue

            month_key = meeting_dt.strftime("%Y-%m")
            result[month_key] = MeetingProbs(
                probs      = probs,
                fetched_at = now,
                sources    = ["fedwatch"],
                confidence = 0.90,
            )
            log.debug("FedWatch: meeting %s probs=%s", month_key, probs)

        except Exception as exc:
            log.debug("Meeting parse error: %s", exc)

    log.info("FedWatch: loaded %d upcoming meetings.", len(result))
    return result


# ── Source 2: CME ZQ Futures (direct) ────────────────────────────────────────

async def _fetch_zq_price(year: int, month: int) -> float | None:
    """
    Fetch the settlement price of a specific ZQ (30-day Fed Funds futures) contract.

    ZQ price = 100 - implied Fed Funds rate
    e.g. ZQ price 94.67 → implied rate 5.33%

    The rate implied by a futures contract for month M tells us the
    market's expectation of the average Fed Funds rate during month M.
    For a meeting in month M, the ZQ contract for month M gives us
    the post-meeting rate expectation.
    """
    month_code = _CME_MONTH.get(month, "")
    if not month_code:
        return None

    # Year is 2-digit in CME contract codes
    yr2      = str(year)[-2:]
    contract = f"ZQ{month_code}{yr2}"
    cache_key = f"cme:zq:{contract}"

    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        url = (
            f"https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/305/G/{contract}"
            f"?quoteCodes=null&_=1"
        )
        http = await _get_http_client()
        headers = {
            "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0.0.0 Safari/537.36",
            "Accept":           "application/json, */*; q=0.01",
            "Referer":          "https://www.cmegroup.com/markets/interest-rates/"
                                "cme-fedwatch-tool.html",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = await asyncio.wait_for(
                http.get(url, headers=headers),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            log.debug("ZQ %s: request timeout", contract)
            return None
        if resp.status_code in (403, 429):
            log.debug("ZQ %s: CME returned %d — CDN block", contract, resp.status_code)
            return None
        if "html" in resp.headers.get("content-type", ""):
            log.debug("ZQ %s: CME returned HTML instead of JSON — CDN block", contract)
            return None
        resp.raise_for_status()
        data = resp.json()

        quotes = data.get("quotes", [])
        if quotes:
            last = float(quotes[0].get("last", 0) or quotes[0].get("settle", 0))
            if last > 0:
                _cache.set(cache_key, last, ttl=_TTL_FUTURES)
                log.debug("ZQ %s: %.4f (implied rate %.2f%%)", contract, last, 100 - last)
                return last

    except Exception as exc:
        log.debug("ZQ fetch failed for %s: %s", contract, exc)

    return None


def _zq_to_probs(
    pre_meeting_rate: float,
    post_meeting_price: float,
    current_rate: float,
) -> dict[str, float]:
    """
    Convert ZQ futures prices to rate change probabilities.

    The futures contract settles at 100 - average_rate_for_month.
    For a meeting partway through the month, the implied rate reflects
    a blend of pre-meeting and post-meeting rates.

    We assume the meeting is the only rate-change event in the month and
    solve for the post-meeting rate implied by the futures price.

    Args:
        pre_meeting_rate:   Current Fed Funds rate (%)
        post_meeting_price: ZQ settlement price for meeting month
        current_rate:       Same as pre_meeting_rate (explicit for clarity)

    Returns:
        Probability distribution over HOLD, CUT_25, CUT_50, HIKE_25
    """
    if post_meeting_price <= 0:
        return {"HOLD": 1.0}

    implied_rate = 100 - post_meeting_price

    # Rate change expected (in basis points)
    change_bps = round((implied_rate - current_rate) * 100)

    # Build a simple probability distribution around the implied change
    # by assuming outcomes are probabilistically weighted by proximity
    possible = list(OUTCOME_BPS.keys())
    distances = {k: abs(OUTCOME_BPS[k] - change_bps) for k in possible}
    min_dist  = min(distances.values())

    if min_dist == 0:
        # Futures are pricing exactly one outcome
        winner = min(distances, key=distances.get)
        return {winner: 1.0}

    # Weight inversely by distance, softmax-style
    weights = {k: 1.0 / max(d, 1) for k, d in distances.items()}
    total   = sum(weights.values())
    probs   = {k: w / total for k, w in weights.items()}

    # Filter outcomes with < 2% probability
    probs = {k: v for k, v in probs.items() if v >= 0.02}
    total = sum(probs.values())
    probs = {k: v / total for k, v in probs.items()}

    return probs


# ── Source 3: WSJ Fed Tracker ─────────────────────────────────────────────────

async def _fetch_wsj_probs() -> dict[str, float] | None:
    """
    Scrape the WSJ Fed rate tracker for the next meeting's implied probabilities.
    This is a public page — no subscription required for the summary data.

    Returns a probability dict for the NEXT meeting only, or None.
    Cached for 10 minutes.
    """
    cache_key = "wsj:fed_probs"
    cached    = _cache.get(cache_key)
    if cached:
        return cached

    try:
        url = "https://www.wsj.com/economy/central-banking/fed-rate-monitor-tool"
        http = await _get_http_client()
        resp = await http.get(url, headers={
            "Accept": "text/html,application/xhtml+xml",
        })
        # WSJ may 403 without cookies — that's fine, it's just a backup
        if resp.status_code != 200:
            return None
        html = resp.text

        probs = _parse_wsj_html(html)
        if probs:
            _cache.set(cache_key, probs, ttl=_TTL_WSJ)
            log.debug("WSJ Fed probs: %s", probs)
        return probs

    except Exception as exc:
        log.debug("WSJ fetch failed: %s", exc)
        return None


def _parse_wsj_html(html: str) -> dict[str, float] | None:
    """
    Extract rate change probabilities from WSJ Fed Monitor page.
    Looks for percentage values near outcome labels in the page source.
    """
    try:
        patterns = [
            (r"No Change[^%\d]*?(\d+(?:\.\d+)?)\s*%", "HOLD"),
            (r"Cut 25[^%\d]*?(\d+(?:\.\d+)?)\s*%",    "CUT_25"),
            (r"Cut 50[^%\d]*?(\d+(?:\.\d+)?)\s*%",    "CUT_50"),
            (r"Hike 25[^%\d]*?(\d+(?:\.\d+)?)\s*%",   "HIKE_25"),
            (r"(\d+(?:\.\d+)?)\s*%[^%]*?No Change",   "HOLD"),
            (r"(\d+(?:\.\d+)?)\s*%[^%]*?unchanged",   "HOLD"),
        ]

        result = {}
        for pattern, label in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match and label not in result:
                val = float(match.group(1))
                if 0 < val <= 100:
                    result[label] = val / 100

        if result:
            total = sum(result.values())
            if total > 0:
                return {k: min(1.0, max(0.0, v / total)) for k, v in result.items()}

    except Exception as exc:
        log.debug("WSJ parse error: %s", exc)

    return None


# ── Source fusion ─────────────────────────────────────────────────────────────

def _fuse_sources(
    fedwatch:        dict[str, float] | None,
    zq_probs:        dict[str, float] | None,
    wsj:             dict[str, float] | None,
    kalshi_implied:  dict[str, float] | None = None,
    fedwatch_source: str = "fedwatch",
    kalshi_market_price: float | None = None,
    model_fair_value:    float | None = None,
    suppress_divergence_check: bool = False,
) -> tuple[dict[str, float], float, list[str], str]:
    """
    Combine probability estimates from multiple sources into a blended distribution.

    Weighting when Kalshi-implied prices are present:
      Kalshi-implied: 65% — live market prices on the exchange we trade; highest weight
      FedWatch:       25% — CME FedWatch authenticated API
      second source:  08% — T-bill term structure (FRED DTB3/DTB6/DTB1YR) filling the
                            slot previously occupied by ZQ futures (CDN-blocked since 2024)
      WSJ:            02% — sanity check only

    Weighting when Kalshi-implied is absent (CME-only mode, normal FOMC operation):
      FedWatch:       60%
      second source:  30%  (T-bill term structure, or ZQ if somehow available)
      WSJ:            10%

    Note on the second-source slot: ZQ 30-day Fed Funds futures were permanently
    CDN-blocked by CME WAF in mid-2024. The slot is now filled by T-bill term structure
    data from FRED (DTB3/DTB6/DTB1YR). When T-bill data is in the ZQ slot, the
    HOLD-probability divergence check is suppressed (suppress_divergence_check=True)
    because T-bills measure average rates over their life, not single-meeting probabilities.

    Args:
      fedwatch_source:           label for the primary external source slot. One of:
        "fedwatch"               — real CME FedWatch JSON (conf 0.90 multi-source)
        "fred_futures"           — FRED FF1/FF2/FF3 market-implied rates (conf 0.80 solo)
        "fred_model"             — FRED DFEDTARU + heuristic probs (conf 0.65 solo)
      suppress_divergence_check: skip the HOLD-probability divergence cap when the ZQ
                                 slot carries T-bill data (set True by the FOMC main loop).
      kalshi_market_price:       current Kalshi YES mid-price for this contract (0-1).
      model_fair_value:          blended model fair-value for this contract (0-1).

    Returns:
      (blended_probs, confidence_score, source_list, data_quality)
      data_quality is "fallback_only" when running on FRED static anchor only;
      "ok" otherwise.
    """
    available = {}
    sources   = []

    # Kalshi-implied: highest priority — it IS the market we trade
    if kalshi_implied:
        available["kalshi_implied"] = (kalshi_implied, 0.65)
        sources.append("kalshi_implied")

    # External CME / WSJ / FRED sources: complement Kalshi with path probability data
    if fedwatch:
        w = 0.25 if kalshi_implied else 0.60
        available["fedwatch"] = (fedwatch, w)
        sources.append(fedwatch_source)   # may be "fedwatch", "fred_futures", or "fred_model"
    if zq_probs:
        w = 0.08 if kalshi_implied else 0.30
        available["zq"] = (zq_probs, w)
        sources.append("zq_futures")
    if wsj:
        w = 0.02 if kalshi_implied else 0.10
        available["wsj"] = (wsj, w)
        sources.append("wsj")

    if not available:
        return {"HOLD": 1.0}, 0.20, ["none"], "fallback_only"

    # Renormalise weights to available sources
    total_w = sum(w for _, w in available.values())
    blended: dict[str, float] = {}

    for _src, (probs, weight) in available.items():
        norm_w = weight / total_w
        for outcome, prob in probs.items():
            blended[outcome] = blended.get(outcome, 0.0) + prob * norm_w

    # Normalise
    total = sum(blended.values())
    if total > 0:
        blended = {k: v / total for k, v in blended.items()}

    # Determine data_quality flag: "fallback_only" when running on FRED static
    # anchor only — no genuine forward-looking futures or Kalshi market data.
    # Any real futures source (fedwatch, sr1_sofr, sofr_sr3, fred_futures) or
    # Kalshi-implied prices qualify as "ok".
    _fallback_only = (
        fedwatch_source == "fred_model"
        and not kalshi_implied
        and not zq_probs
    )
    data_quality = "fallback_only" if _fallback_only else "ok"

    # Compute confidence
    if kalshi_implied:
        # Live Kalshi prices are always fresh and directly relevant.
        # Confidence is 0.92 regardless of the external source quality, because
        # Kalshi (65% weight) dominates the blend. The external source is only
        # a path-probability smoother for thin / unlisted strikes.
        confidence = 0.92 if len(available) >= 2 else 0.85
    elif len(available) == 1:
        # Solo source confidence by quality tier
        _solo_conf = {
            "fedwatch":     0.82,  # CME FedWatch alone — permanent state (ZQ CDN-blocked since 2024)
            "sr1_sofr":     0.85,  # CME SR1 SOFR futures — professional FF replacement
            "sofr_sr3":     0.78,  # SOFR SR3 via CME API — 3-month granularity
            "fred_futures": 0.80,  # FRED FF1/2/3 — genuine market-implied data
            "fred_model":   0.65,  # FRED DFEDTARU + heuristic probs — weakest
        }
        confidence = _solo_conf.get(fedwatch_source, 0.70)
        log.debug(
            "Only one source available (%s) — confidence %.2f",
            fedwatch_source, confidence,
        )
    else:
        # Multiple external sources (CME FedWatch + ZQ cross-check, etc.)
        _base_conf = {
            "fedwatch":     0.90,
            "sr1_sofr":     0.85,  # CME SR1 SOFR futures — professional FF replacement
            "sofr_sr3":     0.80,  # SOFR SR3 via CME API — 3-month granularity
            "fred_futures": 0.82,  # better than solo but below real FedWatch
            "fred_model":   0.72,
        }
        confidence = _base_conf.get(fedwatch_source, 0.90)
        # Check divergence between FedWatch and ZQ on HOLD probability.
        # Suppressed when zq_probs slot carries T-bill term structure data —
        # T-bills measure average rates over their life, not a single-meeting
        # probability, so their HOLD numbers naturally differ from FedWatch.
        if not suppress_divergence_check:
            fw_hold = (fedwatch or {}).get("HOLD", 0)
            zq_hold = (zq_probs or {}).get("HOLD", 0)
            if fw_hold and zq_hold:
                divergence = abs(fw_hold - zq_hold)
                if divergence > DIVERGENCE_THRESHOLD:
                    confidence = min(confidence, 0.75)
                    log.warning(
                        "Source divergence: %s HOLD=%.3f vs ZQ HOLD=%.3f "
                        "(diff=%.3f > threshold=%.3f). Confidence reduced to %.2f.",
                        fedwatch_source, fw_hold, zq_hold,
                        divergence, DIVERGENCE_THRESHOLD, confidence,
                    )

    return blended, confidence, sources, data_quality


# ── Ticker parsing ────────────────────────────────────────────────────────────

# Known Kalshi FOMC ticker patterns and their outcome mappings
# Kalshi tickers vary by meeting — these are the common suffixes
_OUTCOME_PATTERNS = [
    (r"HOLD|UNCHANGED|SAME",    "HOLD"),
    (r"CUT.*?50|DOWN.*?50",     "CUT_50"),
    (r"CUT.*?25|DOWN.*?25",     "CUT_25"),
    (r"CUT",                    "CUT_25"),    # generic cut = 25bps
    (r"HIKE.*?50|UP.*?50",      "HIKE_50"),
    (r"HIKE.*?25|UP.*?25",      "HIKE_25"),
    (r"HIKE|RAISE",             "HIKE_25"),   # generic hike = 25bps
    (r"LOWER",                  "CUT_25"),
    (r"RAISE",                  "HIKE_25"),
]



def parse_fomc_ticker(ticker: str) -> dict | None:
    """
    Parse a Kalshi FOMC ticker into meeting date and outcome.
    Handles target-rate format: KXFED-27APR-T4.25
    Also handles legacy format: FOMC-25JUN18-HOLD
    """
    t = ticker.upper()
    if "FOMC" not in t and "FED" not in t:
        return None

    # Target-rate format: KXFED-27APR-T4.25 or KXFED-27APR27-T4.25
    import re as _re
    tr_match = _re.search(r"(\d{2})([A-Z]{3})(?:\d{2})?-T(\d+\.\d+|\d+)", t)
    if tr_match:
        try:
            yy, mon, rate_str = tr_match.groups()
            year  = 2000 + int(yy)
            month = datetime.strptime(mon, "%b").month
            target_rate = float(rate_str)
            # Current Fed Funds rate upper bound ~4.25-4.50
            # Map target rate to outcome by bps difference
            current_rate = _current_fed_rate  # set by set_current_fed_rate() from FRED
            change_bps = round((target_rate - current_rate) * 100 / 25) * 25
            outcome_map = {
                0: "HOLD", -25: "CUT_25", -50: "CUT_50",
                -75: "CUT_75", -100: "CUT_100",
                25: "HIKE_25", 50: "HIKE_50",
            }
            outcome = outcome_map.get(change_bps)
            if outcome is None:
                # Round to nearest known outcome
                closest = min(outcome_map.keys(), key=lambda x: abs(x - change_bps))
                outcome = outcome_map[closest]
            return {
                "meeting": f"{year}-{month:02d}",
                "outcome": outcome,
                "ticker": ticker,
                "target_rate": target_rate,
            }
        except Exception as exc:
            log.debug("Target-rate ticker parse error %s: %s", ticker, exc)
            return None

    # Legacy format: FOMC-25JUN18-HOLD or KXFED-25APR30-CUT25
    date_match = _RE_DATE_PATTERN.search(t)
    if not date_match:
        return None
    try:
        yy, mon, dd = date_match.groups()
        year  = 2000 + int(yy)
        month = datetime.strptime(mon, "%b").month
        meeting_key = f"{year}-{month:02d}"
    except ValueError:
        return None
    outcome = None
    suffix  = t.split("-")[-1]
    for pattern, label in _OUTCOME_PATTERNS:
        if re.search(pattern, suffix):
            outcome = label
            break
    if outcome is None:
        return None
    return {"meeting": meeting_key, "outcome": outcome, "ticker": ticker}


# ── Source 3b: CME SR1 SOFR Futures (professional FF-futures replacement) ─────

async def _fetch_sofr_sr1_meetings() -> "dict[str, MeetingProbs] | None":
    """
    Derive per-meeting FOMC probability distributions from CME SR1 (1-month SOFR)
    futures prices.

    SR1 is the professional successor to the discontinued FF1/FF2/FF3 30-day Fed
    Funds futures.  Settlement = average SOFR over the contract month ≈ expected
    Fed Funds rate for that month.

    Price format: 100 − annualized_rate  (e.g. 94.50 → 5.50%).

    Meeting probability derivation:
      Compare adjacent contract month rates.  The rate difference divided by 25 bp
      (standard hike/cut size) gives the probability of a 25-bp move at the meeting
      between those two months:

        implied_cut_prob = (prev_month_rate − next_month_rate) / 0.25

      Result is clamped to [0, 1].

    Confidence: 0.85  (above FRED EFFR synthetic, below authenticated FedWatch)
    Cache TTL:  300 s (same as FedWatch)
    """
    _TTL_SR1 = 300

    cache_key = "cme:sr1:meetings"
    cached    = _cache.get(cache_key)
    if cached is not None:
        return cached

    _cme_auth_url = os.getenv(
        "CME_FEDWATCH_AUTH_URL",
        "https://auth.cmegroup.com/as/token.oauth2",
    ).strip()
    _cme_key_name = os.getenv("CME_FEDWATCH_API_KEY_NAME", "").strip()
    _cme_password = os.getenv("CME_FEDWATCH_API_PASSWORD", "").strip()

    if not (_cme_key_name and _cme_password):
        log.debug("SR1 unavailable — CME credentials not configured, skipping")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        ) as http:
            # Step 1: OAuth2 client credentials → CME JWT
            tok_resp = await http.post(
                _cme_auth_url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     _cme_key_name,
                    "client_secret": _cme_password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            cme_jwt = tok_resp.json().get("access_token", "")
            if not cme_jwt:
                log.debug(
                    "SR1: CME token step returned %d — skipping",
                    tok_resp.status_code,
                )
                return None

            # Step 2: Fetch SR1 quotes — try primary URL then alternate
            sr1_headers = {
                "Authorization":           f"Bearer {cme_jwt}",
                "Accept":                  "application/json",
                "CME-Application-Name":    "EdgePulse",
                "CME-Application-Version": "1.0",
                "CME-Request-ID":          f"ep-sr1-{int(time.time())}",
                "User-Agent":              "EdgePulse/1.0",
            }
            sr1_data: dict | None = None
            for url in [
                "https://markets.api.cmegroup.com/quotes/futures/SR1",
                "https://markets.api.cmegroup.com/v1/futures/products/SR1/quotes",
            ]:
                try:
                    r = await http.get(url, headers=sr1_headers)
                    if r.status_code in (403, 404):
                        log.debug(
                            "SR1: %s returned %d — trying next URL",
                            url, r.status_code,
                        )
                        continue
                    if r.status_code != 200:
                        log.debug(
                            "SR1: %s returned unexpected status %d",
                            url, r.status_code,
                        )
                        continue
                    # Log response shape for parser tuning
                    raw_json = r.json()
                    log.debug(
                        "SR1 response from %s: top-level keys=%s",
                        url, list(raw_json.keys()) if isinstance(raw_json, dict) else type(raw_json).__name__,
                    )
                    sr1_data = raw_json
                    break
                except Exception as url_exc:
                    log.debug("SR1: request to %s failed: %s", url, url_exc)

            if sr1_data is None:
                log.debug("SR1 unavailable — skipping")
                return None

        # Step 3: Parse SR1 quotes → (month, implied_rate) pairs
        # Support multiple response shapes:
        #   {"quotes": [...]}
        #   {"data": [...]}
        #   [...]  (bare list)
        quotes_list: list = []
        if isinstance(sr1_data, list):
            quotes_list = sr1_data
        elif isinstance(sr1_data, dict):
            for key in ("quotes", "data", "items", "results"):
                if isinstance(sr1_data.get(key), list):
                    quotes_list = sr1_data[key]
                    break

        if not quotes_list:
            log.debug("SR1: could not locate quotes list in response — skipping")
            return None

        # Build (calendar_month_datetime, rate_pct) from each quote entry
        now           = datetime.now(timezone.utc)
        month_rates:  list[tuple[datetime, float]] = []

        for q in quotes_list:
            if not isinstance(q, dict):
                continue
            # Price field: try common CME field names
            price_raw = None
            for field in ("last", "settle", "close", "lastPrice", "settlementPrice", "price"):
                val = q.get(field)
                if val is not None:
                    try:
                        price_raw = float(val)
                        break
                    except (TypeError, ValueError):
                        continue
            if price_raw is None or not (80.0 <= price_raw <= 100.0):
                continue

            implied_rate = 100.0 - price_raw   # annualized SOFR rate (%)

            # Contract month: try common field names
            expiry_raw = None
            for field in ("expirationDate", "expiry", "maturityDate",
                          "contractMonth", "month", "tradeDate", "code"):
                val = q.get(field)
                if val is not None:
                    expiry_raw = str(val)
                    break
            if not expiry_raw:
                continue

            # Parse expiry into a datetime: "2026-06", "JUN26", "Jun 2026", "2026-06-30", …
            contract_dt: datetime | None = None
            # Try numeric YYYY-MM or YYYY-MM-DD
            _date_m = re.match(r"(\d{4})-(\d{2})", expiry_raw)
            if _date_m:
                try:
                    contract_dt = datetime(int(_date_m.group(1)), int(_date_m.group(2)), 1,
                                           tzinfo=timezone.utc)
                except ValueError:
                    pass
            # Try CME month-code format: "SR1M6", "SR1N26", codes embedded in strings
            if contract_dt is None:
                _mc_m = re.search(r"([FGHJKMNQUVXZ])(\d{1,2})$", expiry_raw.upper())
                if _mc_m:
                    code_char = _mc_m.group(1)
                    yr_suffix = int(_mc_m.group(2))
                    year_full = 2020 + yr_suffix if yr_suffix < 80 else 1900 + yr_suffix
                    _inv_cme  = {v: k for k, v in _CME_MONTH.items()}
                    mo_num    = _inv_cme.get(code_char)
                    if mo_num:
                        try:
                            contract_dt = datetime(year_full, mo_num, 1, tzinfo=timezone.utc)
                        except ValueError:
                            pass
            # Try "Jun 2026" or "JUN26" textual formats
            if contract_dt is None:
                for fmt in ("%b %Y", "%B %Y", "%b%Y", "%B%Y"):
                    try:
                        contract_dt = datetime.strptime(expiry_raw[:8], fmt).replace(
                            tzinfo=timezone.utc
                        )
                        break
                    except ValueError:
                        continue

            if contract_dt is None:
                log.debug("SR1: could not parse expiry '%s' — skipping contract", expiry_raw)
                continue

            if contract_dt < now - timedelta(days=32):
                continue   # skip expired contracts

            month_rates.append((contract_dt, implied_rate))

        if len(month_rates) < 2:
            log.debug("SR1: fewer than 2 usable contract months — skipping")
            return None

        # Sort chronologically
        month_rates.sort(key=lambda x: x[0])

        # Step 4: Derive meeting probabilities from adjacent month rate differences
        result: dict[str, MeetingProbs] = {}

        for i in range(len(month_rates) - 1):
            prev_dt, prev_rate = month_rates[i]
            next_dt, next_rate = month_rates[i + 1]

            # FOMC meeting is approximately between prev_dt and next_dt.
            # Use next_dt as the meeting month key (post-meeting month).
            meeting_key = next_dt.strftime("%Y-%m")

            rate_diff  = prev_rate - next_rate   # positive = cut, negative = hike
            cut_prob   = rate_diff / 0.25         # probability of a 25-bp cut

            if cut_prob > 1.0 or cut_prob < 0.0:
                log.warning(
                    "SR1: implied_cut_prob=%.4f out of [0,1] for meeting %s "
                    "(prev_rate=%.4f next_rate=%.4f) — skipping",
                    cut_prob, meeting_key, prev_rate, next_rate,
                )
                continue

            cut_prob   = max(0.0, min(1.0, cut_prob))

            # Build probability distribution
            # cut_prob is P(25-bp cut); the remainder is distributed between HOLD
            # and a small HIKE tail (symmetric around zero-move).
            hike_prob  = max(0.0, -rate_diff / 0.25)   # positive only if rate rises
            hold_prob  = max(0.0, 1.0 - cut_prob - hike_prob)

            # If implied move is close to zero, assign small tails
            if abs(rate_diff) < 0.01:
                probs = {"HOLD": 0.85, "CUT_25": 0.08, "HIKE_25": 0.07}
            elif cut_prob >= 0.5:
                # Majority probability on cuts; distribute remaining between HOLD
                probs = {
                    "CUT_25": cut_prob,
                    "HOLD":   hold_prob,
                    "HIKE_25": hike_prob if hike_prob > 0.01 else 0.0,
                }
            else:
                probs = {
                    "HOLD":   hold_prob,
                    "CUT_25": cut_prob,
                    "HIKE_25": hike_prob if hike_prob > 0.01 else 0.0,
                }

            # Strip zero entries and renormalize
            probs = {k: v for k, v in probs.items() if v > 0.0}
            _total = sum(probs.values())
            if _total > 0:
                probs = {k: v / _total for k, v in probs.items()}

            if not _validate_probs(probs, "sr1_sofr", f"meeting={meeting_key}"):
                log.debug("SR1: invalid probs for %s — skipping", meeting_key)
                continue

            result[meeting_key] = MeetingProbs(
                probs      = probs,
                fetched_at = now,
                sources    = ["sr1_sofr"],
                confidence = 0.85,
            )
            log.debug(
                "SR1 SOFR: meeting %s probs=%s (prev_rate=%.4f next_rate=%.4f)",
                meeting_key,
                {k: f"{v:.2f}" for k, v in sorted(probs.items(), key=lambda x: -x[1])},
                prev_rate, next_rate,
            )

        if not result:
            log.debug("SR1: no valid meeting probs derived — skipping")
            return None

        log.info(
            "SR1 SOFR futures: derived meeting probs for %d months (%s)",
            len(result), ", ".join(sorted(result)),
        )
        _cache.set(cache_key, result, ttl=_TTL_SR1)
        return result

    except Exception as exc:
        log.debug("SR1 unavailable — skipping (%s)", exc)
        return None


# ── Source 3c: SOFR SR3 Futures (CME API, same OAuth as FedWatch / SR1) ──────

async def _fetch_sofr_sr3_meetings() -> "dict[str, MeetingProbs] | None":
    """
    Derive per-meeting FOMC probability distributions from CME SR3 (3-month SOFR)
    futures via the CME API (same OAuth2 credentials as FedWatch and SR1).

    SR3 quarterly contracts (H=Mar, M=Jun, U=Sep, Z=Dec) each capture the
    expected 3-month SOFR average for that quarter.  Implied FF rate =
    (100 - price) - 0.05  (SOFR/FFR basis adjustment).

    Confidence: 0.78  (below SR1 due to 3-month granularity; above FRED EFFR synth)
    Cache TTL:  300 s
    """
    _TTL_SR3 = 300

    cache_key = "cme:sr3:meetings"
    cached    = _cache.get(cache_key)
    if cached is not None:
        return cached

    _cme_auth_url = os.getenv(
        "CME_FEDWATCH_AUTH_URL",
        "https://auth.cmegroup.com/as/token.oauth2",
    ).strip()
    _cme_key_name = os.getenv("CME_FEDWATCH_API_KEY_NAME", "").strip()
    _cme_password = os.getenv("CME_FEDWATCH_API_PASSWORD", "").strip()

    if not (_cme_key_name and _cme_password):
        log.debug("SR3 unavailable -- CME credentials not configured, skipping")
        return None

    # SR3 quarterly contract month -> FOMC meeting date for the same quarter.
    # H=Mar, M=Jun, U=Sep, Z=Dec; update each January for the new year.
    _SR3_QUARTER_TO_MEETING: dict[tuple[int, int], str] = {
        (2026, 6):  "2026-06-17",
        (2026, 9):  "2026-09-16",
        (2026, 12): "2026-12-16",
        (2027, 3):  "2027-03-17",
        (2027, 6):  "2027-06-15",
        (2027, 9):  "2027-09-21",
        (2027, 12): "2027-12-14",
    }

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        ) as http:
            # Step 1: OAuth2 client credentials -> CME JWT
            tok_resp = await http.post(
                _cme_auth_url,
                data={
                    "grant_type":    "client_credentials",
                    "client_id":     _cme_key_name,
                    "client_secret": _cme_password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            cme_jwt = tok_resp.json().get("access_token", "")
            if not cme_jwt:
                log.debug(
                    "SR3: CME token step returned %d -- skipping",
                    tok_resp.status_code,
                )
                return None

            # Step 2: Fetch SR3 quotes from CME API
            sr3_headers = {
                "Authorization":           f"Bearer {cme_jwt}",
                "Accept":                  "application/json",
                "CME-Application-Name":    "EdgePulse",
                "CME-Application-Version": "1.0",
                "CME-Request-ID":          f"ep-sr3-{int(time.time())}",
                "User-Agent":              "EdgePulse/1.0",
            }
            sr3_data: dict | None = None
            for url in [
                "https://markets.api.cmegroup.com/quotes/futures/SR3",
                "https://markets.api.cmegroup.com/v1/futures/products/SR3/quotes",
            ]:
                try:
                    r = await http.get(url, headers=sr3_headers)
                    if r.status_code in (403, 404):
                        log.debug(
                            "SR3: %s returned %d -- trying next URL",
                            url, r.status_code,
                        )
                        continue
                    if r.status_code != 200:
                        log.debug(
                            "SR3: %s returned unexpected status %d",
                            url, r.status_code,
                        )
                        continue
                    raw_json = r.json()
                    log.debug(
                        "SR3 response from %s: top-level keys=%s",
                        url, list(raw_json.keys()) if isinstance(raw_json, dict) else type(raw_json).__name__,
                    )
                    sr3_data = raw_json
                    break
                except Exception as url_exc:
                    log.debug("SR3: request to %s failed: %s", url, url_exc)

        if sr3_data is None:
            log.debug("SR3 unavailable -- skipping")
            return None

        # Step 3: Parse SR3 quotes -> (contract_month_dt, implied_rate) pairs
        quotes_list: list = []
        if isinstance(sr3_data, list):
            quotes_list = sr3_data
        elif isinstance(sr3_data, dict):
            for key in ("quotes", "data", "items", "results"):
                if isinstance(sr3_data.get(key), list):
                    quotes_list = sr3_data[key]
                    break

        if not quotes_list:
            log.debug("SR3: could not locate quotes list in response -- skipping")
            return None

        now          = datetime.now(timezone.utc)
        current_rate = _current_fed_rate
        result: dict[str, MeetingProbs] = {}

        for q in quotes_list:
            if not isinstance(q, dict):
                continue

            # Price field -- try common CME field names
            price_raw = None
            for field in ("last", "settle", "close", "lastPrice", "settlementPrice", "price"):
                val = q.get(field)
                if val is not None:
                    try:
                        price_raw = float(val)
                        break
                    except (TypeError, ValueError):
                        continue
            if price_raw is None or not (80.0 <= price_raw <= 100.0):
                continue

            # SR3: implied 3-month SOFR average; subtract 5 bp SOFR/FFR basis
            implied_rate = round((100.0 - price_raw) - 0.05, 4)

            # Contract month -- try common field names
            expiry_raw = None
            for field in ("expirationDate", "expiry", "maturityDate",
                          "contractMonth", "month", "tradeDate", "code"):
                val = q.get(field)
                if val is not None:
                    expiry_raw = str(val)
                    break
            if not expiry_raw:
                continue

            # Parse expiry into a datetime
            contract_dt: datetime | None = None
            _date_m = re.match(r"(\d{4})-(\d{2})", expiry_raw)
            if _date_m:
                try:
                    contract_dt = datetime(int(_date_m.group(1)), int(_date_m.group(2)), 1,
                                           tzinfo=timezone.utc)
                except ValueError:
                    pass
            if contract_dt is None:
                _mc_m = re.search(r"([FGHJKMNQUVXZ])(\d{1,2})$", expiry_raw.upper())
                if _mc_m:
                    code_char = _mc_m.group(1)
                    yr_suffix = int(_mc_m.group(2))
                    year_full = 2020 + yr_suffix if yr_suffix < 80 else 1900 + yr_suffix
                    _inv_cme  = {v: k for k, v in _CME_MONTH.items()}
                    mo_num    = _inv_cme.get(code_char)
                    if mo_num:
                        try:
                            contract_dt = datetime(year_full, mo_num, 1, tzinfo=timezone.utc)
                        except ValueError:
                            pass
            if contract_dt is None:
                for fmt in ("%b %Y", "%B %Y", "%b%Y", "%B%Y"):
                    try:
                        contract_dt = datetime.strptime(expiry_raw[:8], fmt).replace(
                            tzinfo=timezone.utc
                        )
                        break
                    except ValueError:
                        continue

            if contract_dt is None:
                log.debug("SR3: could not parse expiry '%s' -- skipping contract", expiry_raw)
                continue
            if contract_dt < now - timedelta(days=32):
                continue   # skip expired contracts

            # Map contract quarter to FOMC meeting date
            meeting_date_str = _SR3_QUARTER_TO_MEETING.get(
                (contract_dt.year, contract_dt.month)
            )
            if not meeting_date_str:
                log.debug(
                    "SR3: no FOMC meeting mapped for %d-%02d -- skipping",
                    contract_dt.year, contract_dt.month,
                )
                continue

            # Inline: rate change -> probability distribution (like _zq_to_probs)
            _change_bps = round((implied_rate - current_rate) * 100)
            _possible   = list(OUTCOME_BPS.keys())
            _distances  = {k: abs(OUTCOME_BPS[k] - _change_bps) for k in _possible}
            _min_d      = min(_distances.values())
            if _min_d == 0:
                _winner = min(_distances, key=_distances.get)
                probs = {_winner: 1.0}
            else:
                _wts   = {k: 1.0 / max(d, 1) for k, d in _distances.items()}
                _tot   = sum(_wts.values())
                probs  = {k: v / _tot for k, v in _wts.items() if v / _tot >= 0.02}
                _tot2  = sum(probs.values())
                probs  = {k: v / _tot2 for k, v in probs.items()}
            if not _validate_probs(probs, "sofr_sr3", f"meeting={meeting_date_str[:7]}"):
                log.debug("SR3: invalid probs for %s -- skipping", meeting_date_str)
                continue

            meeting_key = meeting_date_str[:7]  # "YYYY-MM"
            if meeting_key not in result:
                result[meeting_key] = MeetingProbs(
                    probs      = probs,
                    fetched_at = now,
                    sources    = ["sofr_sr3"],
                    confidence = 0.78,
                )
                log.debug(
                    "SR3: meeting %s price=%.4f implied_ff=%.4f%% probs=%s",
                    meeting_key, price_raw, implied_rate,
                    {k: f"{v:.2f}" for k, v in sorted(probs.items(), key=lambda x: -x[1])},
                )

        if not result:
            log.debug("SR3: no future meeting probs derived -- skipping")
            return None

        log.info(
            "SOFR SR3 futures: derived meeting probs for %d months (%s)",
            len(result), ", ".join(sorted(result)),
        )
        _cache.set(cache_key, result, ttl=_TTL_SR3)
        return result

    except Exception as exc:
        log.debug("SOFR SR3 fetch failed: %s", exc)
        return None


# ── Source 3d: Treasury Bill Term Structure (FRED DTB3/DTB6/DTB1YR) ──────────

async def _fetch_tbill_term_structure_meetings() -> "dict[str, MeetingProbs] | None":
    """
    Derive per-meeting FOMC probability distributions from the U.S. Treasury
    bill yield curve (3-month, 6-month, 1-year secondary market rates).

    T-bill yields are independently forward-looking: the market prices each
    bill to reflect the expected average Fed Funds rate over the bill's life.
    They are sourced from FRED (DTB3, DTB6, DTB1YR) — free, no auth beyond our
    existing FRED_API_KEY.

    Method:
      1. Fetch the three T-bill yields.
      2. Adjust for the typical T-bill / EFFR basis spread (~4 bp bill discount).
      3. Linearly interpolate the implied FF rate at each FOMC meeting horizon.
      4. Convert to per-meeting probability distributions.

    Confidence: 0.72  (independent instrument, coarser than per-meeting futures;
                        combined with FedWatch lifts fuse confidence to 0.90)
    Cache TTL: 900 s  (T-bills update daily; 15-min cache is sufficient)
    """
    _TTL_TBILL   = 900
    cache_key    = "fred:tbill:meetings"
    cached       = _cache.get(cache_key)
    if cached is not None:
        return cached

    fred_key = os.getenv("FRED_API_KEY", "").strip()
    if not fred_key:
        log.debug("T-bill source unavailable -- FRED_API_KEY not configured")
        return None

    try:
        loop = asyncio.get_event_loop()

        def _fetch_yields() -> "dict[str, float] | None":
            import requests as _req
            yields = {}
            for sid, horizon_months in [("DTB3", 3), ("DTB6", 6), ("DTB1YR", 12)]:
                url = (
                    f"https://api.stlouisfed.org/fred/series/observations"
                    f"?series_id={sid}&api_key={fred_key}&file_type=json"
                    f"&sort_order=desc&limit=5"
                )
                try:
                    r = _req.get(url, headers={"User-Agent": "EdgePulse/1.0"}, timeout=8)
                    if r.status_code != 200:
                        log.debug("T-bill %s: FRED returned %d", sid, r.status_code)
                        continue
                    obs = r.json().get("observations", [])
                    for o in obs:
                        if o.get("value") not in (".", None, ""):
                            yields[horizon_months] = float(o["value"])
                            break
                except Exception as exc:
                    log.debug("T-bill %s fetch failed: %s", sid, exc)
            return yields if len(yields) >= 2 else None

        raw_yields = await loop.run_in_executor(None, _fetch_yields)
        if not raw_yields:
            log.debug("T-bill term structure: fewer than 2 yields fetched -- skipping")
            return None

        log.debug("T-bill yields fetched: %s", raw_yields)

        # Basis adjustment: T-bills typically trade ~4-5 bp below EFFR due to
        # safety/liquidity premium. Add basis back to get implied FF rate.
        _TBILL_BASIS_PCT = 0.04   # 4 bp
        adj_yields = {m: y + _TBILL_BASIS_PCT for m, y in raw_yields.items()}

        # Anchor at month 0 = current EFFR (from module-level _current_fed_rate)
        current_rate = _current_fed_rate
        anchor = {0: current_rate}
        all_points = {**anchor, **adj_yields}
        sorted_horizons = sorted(all_points.keys())

        now      = datetime.now(timezone.utc)
        result: dict[str, MeetingProbs] = {}
        _cal_src = _FOMC_UPCOMING_LIVE if _FOMC_UPCOMING_LIVE else _FOMC_UPCOMING

        for meeting_date_str in _cal_src:
            try:
                meeting_dt = datetime.strptime(meeting_date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if meeting_dt < now:
                continue

            # Months from today to this meeting
            months_ahead = (meeting_dt - now).days / 30.44

            # Interpolate implied rate at this horizon using the yield curve points
            if months_ahead <= sorted_horizons[0]:
                implied_rate = all_points[sorted_horizons[0]]
            elif months_ahead >= sorted_horizons[-1]:
                implied_rate = all_points[sorted_horizons[-1]]
            else:
                # Linear interpolation between bracketing points
                lo, hi = None, None
                for h in sorted_horizons:
                    if h <= months_ahead:
                        lo = h
                    if h >= months_ahead and hi is None:
                        hi = h
                if lo is None or hi is None or lo == hi:
                    implied_rate = all_points.get(lo or hi, current_rate)
                else:
                    frac = (months_ahead - lo) / (hi - lo)
                    implied_rate = all_points[lo] + frac * (all_points[hi] - all_points[lo])

            # Convert to meeting probability distribution
            # Inline: rate change -> probability distribution (like _zq_to_probs)
            _change_bps = round((implied_rate - current_rate) * 100)
            _possible   = list(OUTCOME_BPS.keys())
            _distances  = {k: abs(OUTCOME_BPS[k] - _change_bps) for k in _possible}
            _min_d      = min(_distances.values())
            if _min_d == 0:
                _winner = min(_distances, key=_distances.get)
                probs = {_winner: 1.0}
            else:
                _wts   = {k: 1.0 / max(d, 1) for k, d in _distances.items()}
                _tot   = sum(_wts.values())
                probs  = {k: v / _tot for k, v in _wts.items() if v / _tot >= 0.02}
                _tot2  = sum(probs.values())
                probs  = {k: v / _tot2 for k, v in probs.items()}
            meeting_key = meeting_dt.strftime("%Y-%m")

            if not _validate_probs(probs, "tbill_term", f"meeting={meeting_key}"):
                log.debug("T-bill term: invalid probs for %s -- skipping", meeting_key)
                continue

            result[meeting_key] = MeetingProbs(
                probs      = probs,
                fetched_at = now,
                sources    = ["tbill_term_structure"],
                confidence = 0.72,
            )
            log.debug(
                "T-bill term: meeting %s  months_ahead=%.1f  implied_ff=%.4f%%  probs=%s",
                meeting_key, months_ahead, implied_rate,
                {k: f"{v:.2f}" for k, v in sorted(probs.items(), key=lambda x: -x[1])},
            )

        if not result:
            log.debug("T-bill term structure: no future meeting probs derived -- skipping")
            return None

        log.info(
            "T-bill term structure: derived meeting probs for %d months (%s)",
            len(result), ", ".join(sorted(result)),
        )
        _cache.set(cache_key, result, ttl=_TTL_TBILL)
        return result

    except Exception as exc:
        log.debug("T-bill term structure fetch failed: %s", exc)
        return None


async def _fetch_fred_futures_meetings() -> dict[str, MeetingProbs] | None:
    """
    Fetch FRED 30-Day Fed Funds Futures (FF1/FF2/FF3) and derive per-meeting
    probability distributions.

    NOTE (2026-04): FRED discontinued the FF1/FF2/FF3 CSV series.  The CSV
    endpoint returns a 404 HTML page for all three IDs.  The FRED JSON API also
    reports 'series does not exist' for FF1, FF2, FF3, FF1M, FF2M, FF3M.

    Fallback chain implemented here:
      1. FRED JSON API for FF1/FF2/FF3 (in case they are reinstated)
      2. EFFR (Effective Federal Funds Rate, series EFFR) — daily actual rate.
         Since we have no forward futures prices, we use EFFR as the current-rate
         anchor and build a tight hold/small-cut distribution for near-term meetings.
         This is weaker than actual futures but better than nothing.

    Returns None on complete failure so the caller falls through to the
    static FRED heuristic fallback.

    Cache TTL: 1 hour (_TTL_FRED_FUTURES).
    """
    cache_key = "fred:futures_meetings"
    cached    = _cache.get(cache_key)
    if cached:
        return cached

    fred_key = os.getenv("FRED_API_KEY", "")
    now         = datetime.now(timezone.utc)
    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # FF1 = 1-month ahead (next contract month), FF2 = 2-months, FF3 = 3-months.
    ff_series = ["FF1", "FF2", "FF3"]

    # Build mapping: series → target calendar month
    target_months: list[datetime] = []
    dt = current_month
    for _ in range(len(ff_series)):
        dt = (dt + timedelta(days=32)).replace(day=1)
        target_months.append(dt)

    # ── Attempt 1: FRED CSV endpoint (unauthenticated) ────────────────────────
    # These series were discontinued but try anyway in case FRED reinstates them.
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=True,
        limits=httpx.Limits(max_connections=3, max_keepalive_connections=2),
        headers={"User-Agent": "Mozilla/5.0 (compatible; kalshi-bot/3.0)"},
    ) as http:
        csv_tasks = [
            http.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}")
            for sid in ff_series
        ]
        csv_responses = await asyncio.gather(*csv_tasks, return_exceptions=True)

    # Parse CSV responses — each should be "DATE,VALUE" rows
    ff_prices: dict[str, float | None] = {}
    for sid, resp in zip(ff_series, csv_responses):
        ff_prices[sid] = None
        if isinstance(resp, Exception):
            log.debug("FRED FF CSV: %s fetch error: %s", sid, resp)
            continue
        try:
            if resp.status_code != 200:
                log.debug("FRED FF CSV: %s returned HTTP %d (series may be discontinued)",
                          sid, resp.status_code)
                continue
            # Reject HTML error pages (returned when series doesn't exist)
            ct = resp.headers.get("content-type", "")
            if "html" in ct or resp.text.strip().startswith("<!"):
                log.debug("FRED FF CSV: %s returned HTML (series discontinued)", sid)
                continue
            lines = resp.text.strip().splitlines()
            # Skip header; scan reversed for last non-dot numeric value
            for line in reversed(lines):
                parts = line.split(",")
                if len(parts) == 2 and parts[1].strip() not in (".", ""):
                    price = float(parts[1].strip())
                    if 80.0 <= price <= 100.0:   # sanity: rate 0-20%
                        ff_prices[sid] = price
                        log.debug(
                            "FRED %s CSV: price=%.4f → implied rate=%.4f%%",
                            sid, price, 100 - price,
                        )
                    break
        except Exception as exc:
            log.debug("FRED FF CSV: %s parse error: %s", sid, exc)

    # ── Attempt 2: FRED JSON API (authenticated) for FF series ────────────────
    # Try JSON API in case the series exist there but not on CSV endpoint.
    if fred_key and ff_prices.get("FF1") is None:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as http:
                json_tasks = [
                    http.get(
                        # FRED requires api_key as query param; no header auth supported — accepted risk
                        "https://api.stlouisfed.org/fred/series/observations"
                        f"?series_id={sid}&api_key={fred_key}"
                        "&sort_order=desc&limit=5&file_type=json"
                    )
                    for sid in ff_series
                ]
                json_responses = await asyncio.gather(*json_tasks, return_exceptions=True)

            for sid, resp in zip(ff_series, json_responses):
                if isinstance(resp, Exception) or resp.status_code != 200:
                    continue
                j = resp.json()
                if j.get("error_message"):
                    log.debug("FRED FF JSON: %s — %s", sid, j["error_message"])
                    continue
                for o in j.get("observations", []):
                    val = o.get("value", ".")
                    if val != ".":
                        price = float(val)
                        if 80.0 <= price <= 100.0:
                            ff_prices[sid] = price
                            log.debug("FRED %s JSON: price=%.4f → rate=%.4f%%",
                                      sid, price, 100 - price)
                        break
        except Exception as exc:
            log.debug("FRED FF JSON API error: %s", exc)

    # ── Attempt 3: EFFR fallback ───────────────────────────────────────────────
    # If FF series are unavailable, fetch EFFR (Effective Federal Funds Rate).
    # EFFR is the daily ACTUAL rate (not a futures price), so we cannot derive
    # forward expectations from it directly.  Instead we build a near-current-rate
    # distribution anchored to EFFR to fill in for missing futures data.
    # This is tagged as "fred_futures" source but with lower confidence (0.55).
    if ff_prices.get("FF1") is None:
        effr_rate: float | None = None
        if fred_key:
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
                    r = await http.get(
                        # FRED requires api_key as query param; no header auth supported — accepted risk
                        "https://api.stlouisfed.org/fred/series/observations"
                        f"?series_id=EFFR&api_key={fred_key}"
                        "&sort_order=desc&limit=5&file_type=json"
                    )
                if r.status_code == 200:
                    for o in r.json().get("observations", []):
                        if o.get("value", ".") != ".":
                            effr_rate = float(o["value"])
                            log.info("FRED EFFR fallback: rate=%.4f%%", effr_rate)
                            break
            except Exception as exc:
                log.debug("FRED EFFR fetch failed: %s", exc)

        if effr_rate is not None:
            # Use EFFR as current rate anchor; synthesize pseudo-prices so the
            # existing _zq_to_probs() pipeline can run.
            # We model 3 months of near-current-rate expectations with slight
            # rate-cut lean (matching current cutting cycle context).
            # Synthesized prices: near-current implied rate with 5/10/15 bp
            # reduction per month as rough forward guidance proxy.
            for i, sid in enumerate(ff_series):
                # Each forward month: imply a small incremental cut lean
                implied_rate = effr_rate - (i + 1) * 0.05
                implied_rate = max(0.0, implied_rate)
                ff_prices[sid] = 100.0 - implied_rate
            log.info(
                "FRED FF: using EFFR=%.4f%% to synthesize pseudo-futures prices "
                "(confidence will be lower)",
                effr_rate,
            )

    # ── Build meeting probability distributions ────────────────────────────────
    if ff_prices.get("FF1") is None:
        log.info("FRED FF futures: all sources failed — cannot build meeting probs")
        return None

    result: dict[str, MeetingProbs] = {}
    current_rate = _current_fed_rate
    # Detect whether we're using synthesized EFFR prices (lower confidence)
    _using_effr_synth = all(
        ff_prices.get(s) is not None
        for s in ff_series
    ) and ff_prices.get("FF1") is not None

    for sid, target_dt in zip(ff_series, target_months):
        price = ff_prices.get(sid)
        if price is None:
            continue

        meeting_key = target_dt.strftime("%Y-%m")
        probs = _zq_to_probs(
            pre_meeting_rate    = current_rate,
            post_meeting_price  = price,
            current_rate        = current_rate,
        )
        if not probs:
            continue

        # Confidence: FF1 has tightest time horizon → most reliable; FF3 is looser.
        # EFFR-synthesized prices get a 0.15 confidence penalty (not real futures data).
        conf_map = {"FF1": 0.78, "FF2": 0.73, "FF3": 0.68}
        base_conf = conf_map.get(sid, 0.68)
        result[meeting_key] = MeetingProbs(
            probs      = probs,
            fetched_at = now,
            sources    = ["fred_futures"],
            confidence = base_conf,
        )
        log.debug(
            "FRED %s → meeting %s probs=%s",
            sid, meeting_key,
            {k: f"{v:.2f}" for k, v in sorted(probs.items(), key=lambda x: -x[1])},
        )

    if not result:
        log.info("FRED FF futures: no usable contract prices found")
        return None

    log.info(
        "FRED FF futures: derived meeting probs for %d months (%s)",
        len(result), ", ".join(sorted(result)),
    )
    _cache.set(cache_key, result, ttl=_TTL_FRED_FUTURES)
    return result


async def _fetch_fred_fallback_meetings():
    # Return cached result if available
    cached = _cache.get("fred:fallback_meetings")
    if cached:
        return cached
    import os
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("FRED_API_KEY", "1f665e6cab7f604a5c4a9092c90ca0c1")
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    try:
        http = await _get_http_client()
        # Fetch 400 observations (> 1 year of daily data) so we can reliably
        # detect the rate cycle even when the Fed has been on hold for months.
        url = (  # FRED requires api_key as query param; no header auth supported — accepted risk
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DFEDTARU&api_key={api_key}&file_type=json"
            "&sort_order=desc&limit=400"
        )
        resp = await http.get(url)
        resp.raise_for_status()
        obs = [o for o in resp.json().get("observations", []) if o.get("value") != "."]
        current_rate = float(obs[0]["value"]) if obs else 4.33
        set_current_fed_rate(current_rate)  # keep parse_fomc_ticker in sync
        rates = [float(o["value"]) for o in obs]
        changes = sum(1 for i in range(len(rates)-1) if rates[i] != rates[i+1])
        if changes == 0:
            # No recent moves — steady state; HIKE_50 gets a small tail
            probs = {
                "HOLD": 0.79, "CUT_25": 0.09, "CUT_50": 0.05,
                "CUT_75": 0.03, "CUT_100": 0.01, "HIKE_25": 0.02, "HIKE_50": 0.01,
            }
        elif rates[0] < rates[-1]:
            # Cutting cycle — more weight on larger cuts; hikes very unlikely
            probs = {
                "HOLD": 0.40, "CUT_25": 0.28, "CUT_50": 0.16,
                "CUT_75": 0.10, "CUT_100": 0.04, "HIKE_25": 0.01, "HIKE_50": 0.01,
            }
        else:
            # Hiking cycle — HIKE_50 meaningful; cuts unlikely
            probs = {
                "HOLD": 0.55, "HIKE_25": 0.22, "HIKE_50": 0.05,
                "CUT_25": 0.10, "CUT_50": 0.05, "CUT_75": 0.02, "CUT_100": 0.01,
            }
        log.info("FRED fallback: rate=%.2f%% probs=%s", current_rate, {k: f"{v:.0%}" for k,v in probs.items()})
    except Exception as exc:
        log.warning("FRED fetch failed: %s — using static probs", exc)
        probs = {
            "HOLD": 0.73, "CUT_25": 0.12, "CUT_50": 0.07,
            "CUT_75": 0.04, "CUT_100": 0.01, "HIKE_25": 0.02, "HIKE_50": 0.01,
        }

    # Generate meeting keys: 3 months back + current + next 24 months.
    # Kalshi sometimes keeps markets open for past meetings until settlement.
    # Including recent past months prevents those tickers from being "misses".
    from datetime import timedelta
    result = {}
    # Past 3 months (low confidence — market likely resolved)
    dt = now.replace(day=1)
    for _ in range(3):
        dt = (dt - timedelta(days=1)).replace(day=1)
        result[dt.strftime("%Y-%m")] = MeetingProbs(
            probs=probs, fetched_at=now, sources=["fred_model"], confidence=0.40
        )
    # Current month
    result[now.strftime("%Y-%m")] = MeetingProbs(
        probs=probs, fetched_at=now, sources=["fred_model"], confidence=0.50
    )
    # Next 24 months
    dt = now.replace(day=1)
    for _ in range(24):
        dt = (dt + timedelta(days=32)).replace(day=1)
        result[dt.strftime("%Y-%m")] = MeetingProbs(
            probs=probs, fetched_at=now, sources=["fred_model"], confidence=0.50
        )
    log.info("FRED fallback: generated %d meeting months", len(result))
    _cache.set("fred:fallback_meetings", result, ttl=3600)
    return result

# ── Main public API ───────────────────────────────────────────────────────────

# Current Fed Funds rate — updated by set_current_fed_rate() after each FRED fetch.
# ── Source 0: Kalshi market-implied FOMC probabilities ───────────────────────
# Populated each Intel cycle via inject_kalshi_prices().
# Keys:   KXFED ticker strings (e.g. "KXFED-26JUN-T3.00")
# Values: YES price integer (0–100)
_kalshi_prices:    dict[str, int] = {}
_kalshi_prices_ts: float          = 0.0
_TTL_KALSHI = 150.0   # treat injected prices stale after 2.5 cycles (2× poll interval)


def inject_kalshi_prices(snapshot: dict[str, int]) -> None:
    """
    Feed the current KXFED price snapshot into the FOMC model.

    Called from ep_intel.py each cycle after publishing prices to Redis.
    snapshot: {ticker: yes_price_int} — only KXFED tickers need be included.

    If this is the first injection after a cold start (when the model was
    running on FRED fallback only), the meeting-probs cache is invalidated so
    the next get_meeting_probs() call refreshes with the Kalshi-implied source.
    """
    global _kalshi_prices, _kalshi_prices_ts, _last_full_fetch
    was_empty = not _kalshi_prices
    _kalshi_prices    = {k: int(v) for k, v in snapshot.items() if k.startswith("KXFED-")}
    _kalshi_prices_ts = time.time()
    log.debug("FOMC: injected %d Kalshi KXFED prices", len(_kalshi_prices))

    # On the first successful injection, bust the FOMC cache so the model
    # picks up the live Kalshi source on its next refresh rather than waiting
    # up to TTL_FEDWATCH (5 min) before trying the new source.
    if was_empty and _kalshi_prices:
        _last_full_fetch = None
        log.info("FOMC: Kalshi prices available (%d tickers) — cache invalidated for immediate refresh",
                 len(_kalshi_prices))
    try:
        from ep_health import health as _health
        if _kalshi_prices:
            _health.mark_ok("kalshi_implied", f"{len(_kalshi_prices)} tickers")
        else:
            _health.mark_fail("kalshi_implied", "snapshot contained no KXFED tickers")
    except ImportError:
        pass


def _derive_meeting_probs_from_kalshi(meeting_key: str) -> dict[str, float] | None:
    """
    Derive FOMC probability distribution from live Kalshi KXFED market prices.

    Each KXFED-YYMMM-TX.XX YES price encodes market consensus for that rate
    threshold.  We interpret the ladder as a cumulative distribution and compute
    point probabilities via first differences.

    If the ladder is neither cleanly ascending nor descending (illiquid / stale
    markets), we normalise the raw prices as point estimates directly.

    Returns None if fewer than two KXFED prices are available for the meeting.
    """
    if not _kalshi_prices:
        return None
    if time.time() - _kalshi_prices_ts > _TTL_KALSHI:
        log.debug("FOMC: Kalshi prices stale (%.0fs old) — skipping",
                  time.time() - _kalshi_prices_ts)
        return None

    # Build meeting prefix for ticker matching:  "2026-06" → "KXFED-26JUN"
    try:
        year        = int(meeting_key[:4])
        month       = int(meeting_key[5:7])
        month_abbr  = datetime(year, month, 1).strftime("%b").upper()   # "JUN"
        yy          = f"{year - 2000:02d}"                               # "26"
    except (ValueError, IndexError):
        return None

    ticker_prefix = f"KXFED-{yy}{month_abbr}"

    # Collect (target_rate_float, yes_price_0to1) pairs for this meeting
    rate_prices: list[tuple[float, float]] = []
    for ticker, yes_int in _kalshi_prices.items():
        if not ticker.upper().startswith(ticker_prefix):
            continue
        parsed = parse_fomc_ticker(ticker)
        if not parsed or "target_rate" not in parsed:
            continue
        rate_prices.append((parsed["target_rate"], yes_int / 100.0))

    if len(rate_prices) < 2:
        return None

    rate_prices.sort(key=lambda x: x[0])   # ascending by rate
    rates  = [r for r, _ in rate_prices]
    prices = [p for _, p in rate_prices]

    # Determine ladder direction for the cumulative interpretation
    n_asc  = sum(1 for i in range(1, len(prices)) if prices[i] >= prices[i - 1])
    n_desc = sum(1 for i in range(1, len(prices)) if prices[i] <= prices[i - 1])

    outcome_probs: dict[str, float] = {}

    if n_desc >= n_asc:
        # "YES if rate ≥ T": prices decrease with higher T (most common for KXFED)
        # P(rate exactly = T) ≈ P(rate ≥ T) − P(rate ≥ T+step)
        for i, (rate, prob) in enumerate(rate_prices):
            ticker_key = f"KXFED-{yy}{month_abbr}-T{rate:.2f}"
            parsed = parse_fomc_ticker(ticker_key)
            if not parsed:
                continue
            outcome = parsed.get("outcome")
            if not outcome:
                continue
            if i + 1 < len(rate_prices):
                point = max(0.0, prob - rate_prices[i + 1][1])
            else:
                point = prob   # lowest-rate bucket absorbs all remaining mass
            if point > 0:
                outcome_probs[outcome] = outcome_probs.get(outcome, 0.0) + point
    else:
        # "YES if rate ≤ T": prices increase with higher T
        # P(rate exactly = T) ≈ P(rate ≤ T) − P(rate ≤ T−step)
        for i, (rate, prob) in enumerate(rate_prices):
            ticker_key = f"KXFED-{yy}{month_abbr}-T{rate:.2f}"
            parsed = parse_fomc_ticker(ticker_key)
            if not parsed:
                continue
            outcome = parsed.get("outcome")
            if not outcome:
                continue
            if i > 0:
                point = max(0.0, prob - rate_prices[i - 1][1])
            else:
                point = prob   # lowest-threshold bucket: mass below minimum
            if point > 0:
                outcome_probs[outcome] = outcome_probs.get(outcome, 0.0) + point

    if not outcome_probs:
        return None

    # Clamp individual values before normalizing — non-monotonic Kalshi prices
    # (e.g., price[T3.25] > price[T3.00]) can produce negative point masses.
    outcome_probs = {k: max(0.0, v) for k, v in outcome_probs.items()}
    total = sum(outcome_probs.values())
    if total <= 0:
        return None

    normalized = {k: v / total for k, v in outcome_probs.items()}
    log.debug(
        "Kalshi-implied FOMC probs %s: %s  (n=%d tickers, dir=%s)",
        meeting_key,
        {k: f"{v:.2f}" for k, v in sorted(normalized.items(), key=lambda x: -x[1])},
        len(rate_prices),
        "≥T" if n_desc >= n_asc else "≤T",
    )
    return normalized


# ── Used by parse_fomc_ticker() to map target-rate tickers (e.g. KXFED-26JUL-T3.25)
# to outcome labels (HOLD / CUT_25 / CUT_50 …) relative to the actual rate.
# Defaults to 4.33 (historical midpoint); updated at runtime with the live FRED rate.
_current_fed_rate: float = float(os.getenv("CURRENT_FED_RATE", "3.75"))


def set_current_fed_rate(rate: float) -> None:
    """
    Update the Fed Funds rate used by parse_fomc_ticker for outcome mapping.
    Call this once per Intel cycle after fetching the rate from FRED.
    """
    global _current_fed_rate
    if rate and 0.0 < rate < 20.0:
        _current_fed_rate = rate
        log.debug("FOMC parser: current Fed rate updated to %.2f%%", rate)


# Module-level cache for meeting probabilities (refreshed every TTL)
_meeting_probs: dict[str, MeetingProbs] = {}
_last_full_fetch: datetime | None = None
# Lock prevents concurrent callers from all racing to refresh a cold cache
# (cache stampede): only one coroutine runs the refresh; the rest wait and
# then hit the now-warm cache.
_refresh_lock = asyncio.Lock()

# Live FOMC calendar fetched from the Federal Reserve website.
# None = not yet fetched; populated on first get_meeting_probs() call.
_FOMC_UPCOMING_LIVE: list[str] | None = None
_FOMC_CALENDAR_FETCHED_AT: float = 0.0
_FOMC_CALENDAR_TTL: float = 86400.0  # 24 hours


async def _fetch_fomc_calendar() -> list[str]:
    """
    Fetch upcoming FOMC meeting dates from the Federal Reserve's published
    calendar at https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm

    Returns a sorted list of "YYYY-MM-DD" date strings for meetings on or after
    today.  Caches the result for 24 hours.  Falls back to _FOMC_UPCOMING
    (the hardcoded list) if the fetch fails or returns no usable dates.

    The page structure uses:
      <div class="fomc-meeting__month ..."><strong>Month</strong></div>
      <div class="fomc-meeting__date ...">DD-DD</div>
    within year sections introduced by:
      <a id="...">YYYY FOMC Meetings</a>
    """
    global _FOMC_UPCOMING_LIVE, _FOMC_CALENDAR_FETCHED_AT

    now_ts = time.time()
    # Use FETCHED_AT as the in-flight sentinel: claim the slot BEFORE the first
    # await so all concurrent coroutines that arrive while the HTTP fetch is in
    # progress see a non-zero FETCHED_AT and return early with the fallback.
    # This works because asyncio is single-threaded — assignment is atomic between
    # yield points, and the check+assign below has no await in between.
    if now_ts - _FOMC_CALENDAR_FETCHED_AT < _FOMC_CALENDAR_TTL:
        return _FOMC_UPCOMING_LIVE if _FOMC_UPCOMING_LIVE is not None else _FOMC_UPCOMING
    _FOMC_CALENDAR_FETCHED_AT = now_ts   # claim slot — concurrent calls return above

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url       = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

    try:
        http = await _get_http_client()
        resp = await http.get(url, headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Mozilla/5.0 (compatible; kalshi-bot/3.0)",
        })
        if resp.status_code != 200:
            raise ValueError(f"Fed calendar HTTP {resp.status_code}")

        text = resp.text

        # Split on year section headings: '<a id="...">YYYY FOMC Meetings</a>'
        parts = re.split(r'<a id="\d+">\s*(\d{4})\s*FOMC Meetings</a>', text)

        _month_date_re = re.compile(
            r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z]+)</strong></div>\s*'
            r'<div class="fomc-meeting__date[^"]*"[^>]*>([^<]+)</div>',
            re.DOTALL,
        )

        meetings: list[str] = []
        # parts layout: [pre, year1, content1, year2, content2, ...]
        i = 1
        while i < len(parts) - 1:
            year_str = parts[i].strip()
            content  = parts[i + 1]
            i += 2
            try:
                year_int = int(year_str)
            except ValueError:
                continue

            for month_name, date_range in _month_date_re.findall(content):
                # Parse date range: "28-29", "22 (notation vote)", "16-17*"
                dm = re.search(r"(\d+)[\s\*]*(?:-(\d+))?", date_range.strip())
                if not dm:
                    continue
                # Use the last day of the range as the meeting date
                day = int(dm.group(2)) if dm.group(2) else int(dm.group(1))
                try:
                    month_num    = datetime.strptime(month_name.strip(), "%B").month
                    meeting_date = f"{year_int}-{month_num:02d}-{day:02d}"
                    if meeting_date >= today_str:
                        meetings.append(meeting_date)
                except ValueError:
                    pass

        meetings.sort()

        if meetings:
            _FOMC_UPCOMING_LIVE      = meetings
            _FOMC_CALENDAR_FETCHED_AT = now_ts
            log.info(
                "FOMC calendar: fetched %d upcoming meeting dates from Fed website",
                len(meetings),
            )
            return meetings
        else:
            raise ValueError("Parsed 0 future meetings from Fed calendar")

    except Exception as exc:
        log.warning(
            "FOMC calendar fetch failed (%s) — falling back to hardcoded list",
            exc,
        )

    # Fallback: use the hardcoded _FOMC_UPCOMING list
    fallback = [d for d in _FOMC_UPCOMING if d >= today_str]
    if not fallback:
        log.warning(
            "FOMC calendar: hardcoded _FOMC_UPCOMING has NO future dates — "
            "update _FOMC_UPCOMING annually with the Fed's published schedule.",
        )
    _FOMC_UPCOMING_LIVE = fallback or _FOMC_UPCOMING
    # Don't cache the fallback for the full 24 h — retry sooner
    _FOMC_CALENDAR_FETCHED_AT = now_ts - (_FOMC_CALENDAR_TTL * 0.75)
    return _FOMC_UPCOMING_LIVE


async def get_meeting_probs(meeting_key: str) -> MeetingProbs | None:
    """
    Get the current probability distribution for a specific FOMC meeting.

    Args:
        meeting_key: "YYYY-MM" format, e.g. "2025-06"

    Returns:
        MeetingProbs dataclass or None if data unavailable.
    """
    global _meeting_probs, _last_full_fetch

    # ── One-time startup: fetch live FOMC calendar from Fed website ───────────
    # Run outside the refresh lock — _fetch_fomc_calendar() has its own TTL
    # guard and is safe to call concurrently (idempotent).
    if _FOMC_UPCOMING_LIVE is None:
        await _fetch_fomc_calendar()

    # Fast path: cache warm — no lock needed (read-only dict access is safe)
    now = datetime.now(timezone.utc)
    if (
        _meeting_probs
        and _last_full_fetch is not None
        and (now - _last_full_fetch).total_seconds() <= _TTL_FEDWATCH
    ):
        return _meeting_probs.get(meeting_key)

    # Slow path: cache cold or stale — serialize via lock so only one coroutine
    # runs the full refresh; others wait and hit the warm cache afterward.
    async with _refresh_lock:
        # Re-check under lock (another waiter may have already refreshed)
        now = datetime.now(timezone.utc)
        if (
            _meeting_probs
            and _last_full_fetch is not None
            and (now - _last_full_fetch).total_seconds() <= _TTL_FEDWATCH
        ):
            return _meeting_probs.get(meeting_key)

        # Fetch primary sources concurrently (FedWatch, WSJ, SR3, T-bill term structure)
        fw_task    = fetch_fedwatch_all_meetings()
        wsj_task   = _fetch_wsj_probs()
        sr3_task   = _fetch_sofr_sr3_meetings()
        tbill_task = _fetch_tbill_term_structure_meetings()

        fw_result, wsj_result, sr3_result, tbill_result = await asyncio.gather(
            fw_task, wsj_task, sr3_task, tbill_task, return_exceptions=True
        )

        fw_meetings    = fw_result    if isinstance(fw_result, dict)    else {}
        wsj_probs      = wsj_result   if isinstance(wsj_result, dict)   else None
        # SR3 meetings keyed by "YYYY-MM" -- used as cross-validator when confidence < 0.80
        sr3_meetings   = sr3_result   if isinstance(sr3_result, dict)   else {}
        tbill_meetings = tbill_result if isinstance(tbill_result, dict) else {}
        if tbill_meetings:
            log.info(
                "T-bill term structure available for %d meetings (second source)",
                len(tbill_meetings),
            )
        if sr3_meetings:
            log.info(
                "SOFR SR3 implied rates available for %d meetings (cross-validator)",
                len(sr3_meetings),
            )

        # Gather all ZQ fetches concurrently across all meetings
        # (was sequential: 2 awaits × N meetings → now: all fetches in one gather)
        meeting_keys = list(fw_meetings.keys())
        zq_fetch_args = []
        for mk in meeting_keys:
            year, month = int(mk[:4]), int(mk[5:7])
            prev_month  = month - 1 if month > 1 else 12
            prev_year   = year if month > 1 else year - 1
            zq_fetch_args.append((mk, year, month, prev_year, prev_month))

        zq_tasks = []
        for mk, yr, mo, pyr, pmo in zq_fetch_args:
            zq_tasks.append(_fetch_zq_price(yr, mo))
            zq_tasks.append(_fetch_zq_price(pyr, pmo))

        zq_all = await asyncio.gather(*zq_tasks, return_exceptions=True)

        new_probs: dict[str, MeetingProbs] = {}
        for i, (mk, yr, mo, pyr, pmo) in enumerate(zq_fetch_args):
            fw_mp       = fw_meetings[mk]
            zq_price    = zq_all[i * 2]    if not isinstance(zq_all[i * 2], Exception)    else None
            prev_price  = zq_all[i * 2 + 1] if not isinstance(zq_all[i * 2 + 1], Exception) else None

            zq_probs_dict = None
            try:
                if zq_price and prev_price:
                    current_rate  = 100 - prev_price
                    zq_probs_dict = _zq_to_probs(current_rate, zq_price, current_rate)
            except Exception as exc:
                log.debug("ZQ cross-check failed for %s: %s", mk, exc)

            # Use WSJ only for the first (next) meeting
            wsj_for_meeting = wsj_probs if not new_probs else None

            # Kalshi-implied: derives probability PMF from live KXFED market prices.
            # This is the highest-priority source when available — it IS the market.
            kalshi_probs = _derive_meeting_probs_from_kalshi(mk)
            if kalshi_probs and not _validate_probs(
                kalshi_probs, "kalshi_implied", f"meeting={mk}"
            ):
                log.debug(
                    "get_meeting_probs: Kalshi probs invalid for %s — dropping", mk
                )
                kalshi_probs = None

            # Determine the true source label for the "fedwatch" slot so _fuse_sources
            # emits the correct tag and calibrates confidence appropriately.
            # fw_mp.sources carries the actual origin set by fetch_fedwatch_all_meetings().
            if "sr1_sofr" in fw_mp.sources:
                fw_source_label = "sr1_sofr"
            elif "sofr_sr3" in fw_mp.sources:
                fw_source_label = "sofr_sr3"
            elif "fred_futures" in fw_mp.sources:
                fw_source_label = "fred_futures"
            elif "fred_model" in fw_mp.sources:
                fw_source_label = "fred_model"
            else:
                fw_source_label = "fedwatch"

            # External-only blend: Kalshi market price is what we trade against,
            # not a signal. Including it at 65% weight makes fair_value circular
            # (fair ≈ market) and destroys edge. FedWatch+ZQ+WSJ define fair value.
            # T-bill term structure fills the ZQ slot when ZQ is CDN-blocked.
            # T-bill yields are independently forward-looking; when available they
            # trigger multi-source mode (base_conf 0.82 -> 0.90).
            tbill_mp    = tbill_meetings.get(mk)
            tbill_probs = tbill_mp.probs if tbill_mp else None
            # Use T-bill probs in the ZQ slot (ZQ permanently CDN-blocked since 2024)
            effective_zq = tbill_probs if tbill_probs else zq_probs_dict

            # External-only blend: Kalshi market price is what we trade against,
            # not a signal. Including it at 65% weight makes fair_value circular
            # (fair ≈ market) and destroys edge. FedWatch+T-bill+WSJ define fair value.
            blended, confidence, sources, data_quality = _fuse_sources(
                fw_mp.probs, effective_zq, wsj_for_meeting,
                kalshi_implied             = None,
                fedwatch_source            = fw_source_label,
                suppress_divergence_check  = tbill_probs is not None,
            )
            if tbill_probs and "zq_futures" in sources:
                # Rename the source tag from "zq_futures" to "tbill" for clarity
                sources = [s if s != "zq_futures" else "tbill_term" for s in sources]
                log.debug(
                    "T-bill second source active for %s (conf=%.2f)",
                    mk, confidence,
                )
            if data_quality == "fallback_only":
                log.warning(
                    "FOMC meeting %s: running on FRED static anchor only "
                    "(data_quality=fallback_only) — no genuine forward-looking data",
                    mk,
                )

            # ── SOFR SR3 cross-validation: anchor low-confidence distributions ──
            # When the primary sources yield confidence < 0.80 AND we have a SOFR
            # SR3 implied rate for this meeting month, blend the SR3-derived probs
            # in as an additional signal.  This replaces pure heuristic fallback with
            # genuine market-implied forward rates from a deeply liquid contract.
            # Kalshi-implied source is already high-confidence (≥ 0.85) — skip when
            # it is present so we never override live market prices with SR3 data.
            if (
                confidence < 0.80
                and "kalshi_implied" not in sources
                and sr3_meetings
                and mk in sr3_meetings
            ):
                sr3_mp = sr3_meetings[mk]
                if _validate_probs(sr3_mp.probs, "sofr_sr3_xval", f"meeting={mk}"):
                    # Blend: give the SR3 rate 40% weight as the anchor; existing
                    # blended gets 60%.  This preserves whatever partial signal we
                    # already have while centering the distribution on the SR3 rate.
                    sr3_weight     = 0.40
                    existing_weight = 0.60
                    all_keys = set(blended) | set(sr3_mp.probs)
                    reblended = {
                        k: blended.get(k, 0.0) * existing_weight
                           + sr3_mp.probs.get(k, 0.0) * sr3_weight
                        for k in all_keys
                    }
                    _rb_total = sum(reblended.values())
                    if _rb_total > 0 and _validate_probs(
                        {k: v / _rb_total for k, v in reblended.items()},
                        "sofr_sr3_xval_norm",
                        f"meeting={mk}",
                    ):
                        blended    = {k: v / _rb_total for k, v in reblended.items()}
                        confidence = max(confidence, sr3_mp.confidence)
                        if "sofr_sr3" not in sources:
                            sources = sources + ["sofr_sr3"]
                        log.info(
                            "SOFR SR3 cross-validation applied for %s "
                            "(prior_conf=%.2f → new_conf=%.2f)",
                            mk,
                            confidence,
                            max(confidence, sr3_mp.confidence),
                        )

            # ── Task 4: Confidence calibration based on macro regime quality ──
            _macro_field_count = len(_macro_regime)
            if _macro_field_count >= 6:
                # Fresh, complete macro regime data — slight confidence boost
                confidence = min(0.95, confidence + 0.02)
            elif _macro_field_count == 0:
                # No macro regime data available — reduce certainty
                confidence = max(0.0, confidence - 0.03)

            # VIX > 35: extreme fear — non-Kalshi sources become unreliable
            _vix = _macro_regime.get("vix")
            if _vix is not None and _vix > 35 and "kalshi_implied" not in sources:
                confidence = max(0.0, confidence - 0.10)
                log.debug(
                    "VIX=%.1f (extreme fear): non-Kalshi confidence reduced for %s",
                    _vix, mk,
                )

            # ── Apply macro regime adjustment AFTER all sources are fused ────
            # Only adjust when confidence is strong enough to trust the macro signal.
            if confidence >= 0.70:
                blended = _apply_macro_regime_adjustment(blended, mk)
                if not _validate_probs(blended, "macro_regime_adj", f"meeting={mk}"):
                    log.debug(
                        "get_meeting_probs: macro regime adjustment invalid for %s"
                        " — using pre-adjustment probs",
                        mk,
                    )
                    # Revert: re-run fusion without macro adjustment (blended already set)
                    blended, confidence, sources, data_quality = _fuse_sources(
                        fw_mp.probs, zq_probs_dict, wsj_for_meeting,
                        kalshi_implied  = None,
                        fedwatch_source = fw_source_label,
                    )

            new_probs[mk] = MeetingProbs(
                probs        = blended,
                fetched_at   = now,
                sources      = sources,
                confidence   = confidence,
                data_quality = data_quality,
            )

        if new_probs:
            _meeting_probs   = new_probs
            _last_full_fetch = now
            # Log sources from first FUTURE meeting (skip past months)
            _future = [mp for mk, mp in sorted(new_probs.items()) if mk >= now.strftime("%Y-%m")]
            _sample = _future[0] if _future else list(new_probs.values())[0]
            log.info(
                "FOMC model refreshed: %d meetings  sources=%s  conf=%.2f  "
                "(next_meeting: %s)",
                len(new_probs),
                _sample.sources,
                _sample.confidence,
                _future[0].probs if _future else {},
            )

    return _meeting_probs.get(meeting_key)


def _cumulative_yes_prob(target_rate: float, mp: "MeetingProbs") -> float:
    """
    Compute P(YES) for a "YES if rate ≥ T" KXFED contract.

    KXFED-YYMM-TX contracts pay YES when the Fed Funds rate at the meeting is AT
    OR ABOVE the strike T.  MeetingProbs.probs stores a *point* distribution over
    OUTCOME_BPS levels (HOLD, CUT_25, …).  Summing the point probabilities for all
    outcomes whose implied final rate is ≥ T gives the correct cumulative P(YES).

    Fix: Only sum outcomes where the resulting rate falls within one 25 bp tick of
    the target (i.e., the outcome rate is in [target_rate - 0.25, ∞)).  This
    prevents deep-OTM strikes (e.g. T=2.00% when current=4.25%) from collecting
    every outcome label and wrongly returning ~0.99.

    Hard clamp: returned probabilities are bounded to [0.05, 0.95] so the model
    never claims absolute certainty at the tails, preserving meaningful edge.
    """
    # Lower bound: only include outcomes whose resulting rate is >= target_rate.
    # We treat a 25 bp rounding margin as the minimum meaningful tick — outcomes
    # that land within one tick BELOW the target are excluded (they are OTM).
    raw = sum(
        (mp.get(label) or 0.0)
        for label, bps in OUTCOME_BPS.items()
        if (_current_fed_rate + bps / 100.0) >= target_rate
    )
    # Hard clamp: never return tail certainty — market always retains residual
    # probability of outcomes outside the model's outcome space.
    return max(0.05, min(0.95, raw))


async def fair_value(ticker: str, market_price: float) -> float | None:
    """
    Main entry point for strategy.py.

    Given a Kalshi FOMC ticker, return a fair value probability in [0, 1],
    or None if the ticker isn't an FOMC market or data is unavailable.
    """
    parsed = parse_fomc_ticker(ticker)
    if not parsed:
        return None

    meeting_key = parsed["meeting"]

    mp = await get_meeting_probs(meeting_key)
    if mp is None:
        log.debug("No meeting probs for %s (meeting=%s)", ticker, meeting_key)
        return None

    if "target_rate" in parsed:
        prob = _cumulative_yes_prob(parsed["target_rate"], mp)
    else:
        outcome = parsed.get("outcome")
        if not outcome:
            return None
        prob = mp.get(outcome)
        if prob is None:
            log.debug("Outcome %s not in probs for %s: %s", outcome, ticker, mp.probs)
            return None

    log.debug("FOMC %s → fair_yes=%.4f", ticker, prob)
    return prob


def _staleness_penalty(age_seconds: float, base_confidence: float) -> float:
    """
    Apply a tiered staleness penalty to confidence based on data age.

    Tiers:
      < 30 min  (1800 s):  no penalty  — data is fresh
      30 min – 2 h:        0.80× multiplier  (current behaviour, unchanged)
      2 h – 6 h:           0.50× multiplier  — significantly degraded signal
      > 6 h    (21600 s):  return 0.0 — block signal entirely; data is too old

    Timezone safety: MeetingProbs.fetched_at is always set via
    datetime.now(timezone.utc) and age_seconds() subtracts an equally
    timezone-aware datetime.now(timezone.utc), so the subtraction is safe.
    """
    if age_seconds < 1_800:          # < 30 minutes — fresh
        return base_confidence
    elif age_seconds < 7_200:        # 30 min – 2 hours — mild penalty
        return base_confidence * 0.80
    elif age_seconds < 21_600:       # 2 – 6 hours — significant penalty
        return base_confidence * 0.50
    else:                            # > 6 hours — block signal
        return 0.0


async def get_confidence(ticker: str) -> float:
    """
    Return the current confidence score for a given FOMC ticker.
    Used by strategy.py to pass to the risk manager.
    """
    parsed = parse_fomc_ticker(ticker)
    if not parsed:
        return 0.30

    mp = await get_meeting_probs(parsed["meeting"])
    if mp is None:
        return 0.30

    age = mp.age_seconds()
    if age >= 1_800:   # 30 minutes
        penalised = _staleness_penalty(age, mp.confidence)
        if penalised == 0.0:
            log.warning(
                "FOMC data for %s is %.0fs old (>6h) — confidence blocked to 0.0",
                parsed["meeting"], age,
            )
        else:
            log.debug(
                "FOMC data for %s is %.0fs old — staleness penalty applied, conf=%.2f",
                parsed["meeting"], age, penalised,
            )
        return penalised

    return mp.confidence  # read-only — never mutate shared MeetingProbs


async def fair_value_with_confidence(
    ticker: str, market_price: float
) -> tuple[float | None, float]:
    """
    Return (fair_value, confidence) in a single call.

    For T-format tickers (KXFED-YYMM-TX) the fair value is the CUMULATIVE
    probability P(rate ≥ T) — the sum of all outcome probabilities whose implied
    rate is at or above the strike T.  Using only the point probability for the
    nearest outcome (the previous behaviour) systematically underestimated YES for
    below-current-rate strikes, generating large spurious NO signals.

    Returns (None, 0.30) if the ticker is not an FOMC market or data
    is unavailable.
    """
    parsed = parse_fomc_ticker(ticker)
    if not parsed:
        return None, 0.30

    meeting_key = parsed["meeting"]

    mp = await get_meeting_probs(meeting_key)
    if mp is None:
        return None, 0.30

    if "target_rate" in parsed:
        prob = _cumulative_yes_prob(parsed["target_rate"], mp)
    else:
        outcome = parsed.get("outcome")
        if not outcome:
            return None, 0.30
        prob = mp.get(outcome)
        if prob is None:
            log.debug("FOMC miss: outcome=%s not in probs for %s (keys=%s)",
                      outcome, ticker, list(mp.probs.keys()))
            return None, 0.30

    # Compute effective confidence without mutating shared state.
    # Use tiered staleness penalty: < 30 min no penalty, 30 min-2 h: 0.80×,
    # 2-6 h: 0.50×, > 6 h: 0.0 (signal blocked entirely).
    age = mp.age_seconds()
    if age >= 1_800:   # 30 minutes
        effective_conf = _staleness_penalty(age, mp.confidence)
        if effective_conf == 0.0:
            log.warning(
                "FOMC data for %s is %.0fs old (>6h) — signal blocked "
                "(returning None, 0.0).",
                meeting_key, age,
            )
            return None, 0.0
        log.warning(
            "FOMC data for %s is %.0fs old — staleness penalty applied, "
            "effective confidence %.2f.",
            meeting_key, age, effective_conf,
        )
    else:
        effective_conf = mp.confidence

    if mp.data_quality == "fallback_only":
        log.debug(
            "FOMC %s data_quality=fallback_only — model running on FRED static anchor",
            meeting_key,
        )

    log.debug("FOMC %s → fair_yes=%.4f conf=%.2f data_quality=%s",
              ticker, prob, effective_conf, mp.data_quality)
    return prob, effective_conf
