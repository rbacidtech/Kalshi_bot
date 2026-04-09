"""
models/fomc.py — Fed Funds rate probability model.

Primary signal: CME FedWatch (derived from 30-day Fed Funds futures).
Backup signals: CME ZQ futures direct pricing, WSJ Fed tracker.

Why three sources?
  FedWatch is usually reliable but occasionally lags futures moves by
  minutes during fast markets. Reading ZQ futures directly lets us
  catch those gaps. The WSJ tracker is a useful cross-check and provides
  a human-readable consensus estimate to validate against.

  When all three sources agree → high confidence, full sizing.
  When sources diverge by > DIVERGENCE_THRESHOLD → reduce position size,
  log a warning, and wait for convergence before scaling up.

Meeting awareness:
  Kalshi lists separate contracts for each FOMC meeting date.
  This module maps each meeting date to the correct futures contract
  (ZQ month code) and fetches probabilities for each independently.

Staleness detection:
  If FedWatch data is more than 10 minutes old during market hours,
  the confidence score is reduced automatically until fresh data arrives.

Data sources (all free, no API key required):
  - CME FedWatch: https://www.cmegroup.com
  - CME Futures:  https://www.cmegroup.com (ZQ contracts)
  - WSJ:          https://www.wsj.com/economy/central-banking (public page)
"""

import re
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

import httpx

from .cache import get_cache

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_TIMEOUT        = 8.0   # seconds per HTTP request
_TTL_FEDWATCH   = 300   # 5 min — Fed futures drift slowly intraday
_TTL_FUTURES    = 60    # 1 min — raw ZQ futures update continuously
_TTL_WSJ        = 600   # 10 min — WSJ page updates less frequently
_STALE_MINUTES  = 10    # reduce confidence if data older than this

# Minimum agreement between sources before we treat signal as high-confidence
DIVERGENCE_THRESHOLD = 0.04   # 4 cents — if sources disagree by more, warn

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

    probs:      dict of outcome label → probability, sums to ~1.0
                e.g. {"HOLD": 0.72, "CUT_25": 0.24, "CUT_50": 0.04}
    fetched_at: UTC timestamp of when this data was retrieved
    sources:    which data sources contributed
    confidence: 0-1 score reflecting source agreement and freshness
    """
    probs:      dict[str, float]
    fetched_at: datetime
    sources:    list[str]
    confidence: float = 0.90

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


# ── Source 1: CME FedWatch ────────────────────────────────────────────────────

async def _fetch_fedwatch_raw() -> dict | None:
    """
    Fetch the raw CME FedWatch JSON for all upcoming meetings.
    Returns the full API response dict or None.
    """
    cache_key = "fedwatch:raw"
    cached    = _cache.get(cache_key)
    if cached:
        return cached

    urls = [
        "https://www.cmegroup.com/CmeWS/mvc/MarketData/getFedWatch?selectedDate=&monthlyInterval=1",
        "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
    ]

    http = await _get_http_client()
    for url in urls:
        try:
            resp = await http.get(url)
            resp.raise_for_status()
            data = resp.json()

            _cache.set(cache_key, data, ttl=_TTL_FEDWATCH)
            log.debug("FedWatch raw data fetched from %s", url)
            return data

        except Exception as exc:
            log.debug("FedWatch fetch failed for %s: %s", url, exc)

    log.warning("All FedWatch URLs failed.")
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
        return {}

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
            for fmt in ("%Y-%m-%d", "%Y-%m", "%b %Y", "%B %Y"):
                try:
                    meeting_dt = datetime.strptime(date_str[:10], fmt).replace(
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
        resp = await http.get(url)
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
            return {k: v / total for k, v in result.items()}

    except Exception as exc:
        log.debug("WSJ parse error: %s", exc)

    return None


# ── Source fusion ─────────────────────────────────────────────────────────────

def _fuse_sources(
    fedwatch: dict[str, float] | None,
    zq_probs: dict[str, float] | None,
    wsj:      dict[str, float] | None,
) -> tuple[dict[str, float], float, list[str]]:
    """
    Combine probability estimates from multiple sources.

    Weighting:
      FedWatch:  60% (most complete, all outcomes)
      ZQ futures: 30% (raw market signal, fewer outcomes)
      WSJ:        10% (useful sanity check)

    Returns:
      (blended_probs, confidence_score, source_list)

    Confidence is reduced when:
      - Only one source is available (< 0.70)
      - Sources diverge by > DIVERGENCE_THRESHOLD on any outcome (< 0.80)
      - FedWatch data is stale (< 0.75)
    """
    available = {}
    sources   = []

    if fedwatch:
        available["fedwatch"] = (fedwatch, 0.60)
        sources.append("fedwatch")
    if zq_probs:
        available["zq"] = (zq_probs, 0.30)
        sources.append("zq_futures")
    if wsj:
        available["wsj"] = (wsj, 0.10)
        sources.append("wsj")

    if not available:
        return {"HOLD": 1.0}, 0.20, ["none"]

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

    # Compute confidence
    confidence = 0.90

    if len(available) == 1:
        confidence = 0.70
        log.debug("Only one source available — confidence reduced to %.2f", confidence)

    elif len(available) >= 2:
        # Check divergence between FedWatch and ZQ on HOLD probability
        fw_hold = (fedwatch or {}).get("HOLD", 0)
        zq_hold = (zq_probs or {}).get("HOLD", 0)
        if fw_hold and zq_hold:
            divergence = abs(fw_hold - zq_hold)
            if divergence > DIVERGENCE_THRESHOLD:
                confidence = 0.75
                log.warning(
                    "Source divergence: FedWatch HOLD=%.3f vs ZQ HOLD=%.3f "
                    "(diff=%.3f > threshold=%.3f). Confidence reduced to %.2f.",
                    fw_hold, zq_hold, divergence, DIVERGENCE_THRESHOLD, confidence,
                )

    return blended, confidence, sources


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

    Examples:
        FOMC-25JUN18-HOLD     → {meeting: "2025-06", outcome: "HOLD"}
        FOMC-25MAR19-CUT25    → {meeting: "2025-03", outcome: "CUT_25"}
        KXFED-25JUL30-LOWER   → {meeting: "2025-07", outcome: "CUT_25"}

    Returns None if not a recognisable FOMC ticker.
    """
    t = ticker.upper()

    # Must contain FOMC or FED
    if "FOMC" not in t and "FED" not in t:
        return None

    # Extract date portion: YYMMMDD or YYMMDD
    date_match = _RE_DATE_PATTERN.search(t)
    if not date_match:
        return None

    try:
        yy, mon, dd = date_match.groups()
        year        = 2000 + int(yy)
        month       = datetime.strptime(mon, "%b").month
        meeting_key = f"{year}-{month:02d}"
    except ValueError:
        return None

    # Extract outcome from suffix
    outcome = None
    suffix  = t.split("-")[-1]  # last segment after final dash

    for pattern, label in _OUTCOME_PATTERNS:
        if re.search(pattern, suffix):
            outcome = label
            break

    if outcome is None:
        log.debug("Could not parse outcome from FOMC ticker: %s (suffix=%s)", ticker, suffix)
        return None

    return {"meeting": meeting_key, "outcome": outcome, "ticker": ticker}


# ── Main public API ───────────────────────────────────────────────────────────

# Module-level cache for meeting probabilities (refreshed every TTL)
_meeting_probs: dict[str, MeetingProbs] = {}
_last_full_fetch: datetime | None = None


async def get_meeting_probs(meeting_key: str) -> MeetingProbs | None:
    """
    Get the current probability distribution for a specific FOMC meeting.

    Args:
        meeting_key: "YYYY-MM" format, e.g. "2025-06"

    Returns:
        MeetingProbs dataclass or None if data unavailable.
    """
    global _meeting_probs, _last_full_fetch

    # Refresh if stale or empty
    now = datetime.now(timezone.utc)
    needs_refresh = (
        not _meeting_probs
        or _last_full_fetch is None
        or (now - _last_full_fetch).total_seconds() > _TTL_FEDWATCH
    )

    if needs_refresh:
        # Fetch all three sources concurrently
        fw_task  = fetch_fedwatch_all_meetings()
        wsj_task = _fetch_wsj_probs()

        fw_result, wsj_result = await asyncio.gather(
            fw_task, wsj_task, return_exceptions=True
        )

        fw_meetings = fw_result if isinstance(fw_result, dict) else {}
        wsj_probs   = wsj_result if isinstance(wsj_result, dict) else None

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

            blended, confidence, sources = _fuse_sources(
                fw_mp.probs, zq_probs_dict, wsj_for_meeting
            )

            new_probs[mk] = MeetingProbs(
                probs      = blended,
                fetched_at = now,
                sources    = sources,
                confidence = confidence,
            )

        if new_probs:
            _meeting_probs  = new_probs
            _last_full_fetch = now
            log.info("FOMC model refreshed: %d meetings, sources=%s",
                     len(new_probs),
                     list(new_probs.values())[0].sources if new_probs else [])

    return _meeting_probs.get(meeting_key)


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
    outcome     = parsed["outcome"]

    mp = await get_meeting_probs(meeting_key)
    if mp is None:
        log.debug("No meeting probs for %s (meeting=%s)", ticker, meeting_key)
        return None

    prob = mp.get(outcome)
    if prob is None:
        log.debug("Outcome %s not in probs for %s: %s", outcome, ticker, mp.probs)
        return None

    # Reduce confidence if data is stale — do NOT mutate shared MeetingProbs object
    # (module-level state accessed by multiple components)
    effective_conf = mp.confidence
    if mp.is_stale():
        effective_conf = max(mp.confidence * 0.80, 0.40)
        log.warning("FOMC data for %s is %.0fs old — effective confidence %.2f.",
                    meeting_key, mp.age_seconds(), effective_conf)

    log.debug("FOMC %s → outcome=%s prob=%.4f conf=%.2f sources=%s",
              ticker, outcome, prob, effective_conf, mp.sources)
    return prob


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

    if mp.is_stale():
        return max(mp.confidence * 0.80, 0.40)

    return mp.confidence  # read-only — never mutate shared MeetingProbs


async def fair_value_with_confidence(
    ticker: str, market_price: float
) -> tuple[float | None, float]:
    """
    Return (fair_value, confidence) in a single call.

    Eliminates the dual-await pattern in strategy.py where fair_value()
    and get_confidence() were called separately, each triggering
    get_meeting_probs() (cached, but still two coroutine frames per market).

    Returns (None, 0.30) if the ticker is not an FOMC market or data
    is unavailable.
    """
    parsed = parse_fomc_ticker(ticker)
    if not parsed:
        return None, 0.30

    meeting_key = parsed["meeting"]
    outcome     = parsed["outcome"]

    mp = await get_meeting_probs(meeting_key)
    if mp is None:
        return None, 0.30

    prob = mp.get(outcome)
    if prob is None:
        return None, 0.30

    # Compute effective confidence without mutating shared state
    effective_conf = mp.confidence
    if mp.is_stale():
        effective_conf = max(mp.confidence * 0.80, 0.40)
        log.warning("FOMC data for %s is %.0fs old — effective confidence %.2f.",
                    meeting_key, mp.age_seconds(), effective_conf)

    log.debug("FOMC %s → outcome=%s prob=%.4f conf=%.2f",
              ticker, outcome, prob, effective_conf)
    return prob, effective_conf
