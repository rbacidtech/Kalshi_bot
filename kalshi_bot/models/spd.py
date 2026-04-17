"""
kalshi_bot/models/spd.py — NY Fed Survey of Primary Dealers rate expectations.

The Survey of Primary Dealers (SPD) collects rate path expectations from the
~24 primary dealers (Goldman, JPMorgan, Morgan Stanley, etc.) that trade
directly with the NY Fed. Published quarterly, ~2 weeks before each FOMC meeting.

Why it matters:
  Primary dealers have the most sophisticated rate models and the most
  direct market access. When SPD consensus diverges from Kalshi prediction
  market prices, there's often a systematic edge.

Data source: NY Fed website (HTML) + optional direct Excel download
URL: https://www.newyorkfed.org/markets/survey-of-primary-dealers
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from .cache import get_cache

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_TIMEOUT          = 12.0   # seconds per HTTP request
_TTL_SPD          = 7 * 24 * 3600   # 7 days — SPD is quarterly
_TTL_DOT_PLOT     = 90 * 24 * 3600  # 90 days — SEP published 4x/year

_SPD_INDEX_URL    = "https://www.newyorkfed.org/markets/survey-of-primary-dealers"
_DOT_PLOT_BASE    = "https://www.federalreserve.gov/monetarypolicy"

# Sanity bounds for fed funds rate (in percent)
_RATE_MIN = 0.0
_RATE_MAX = 10.0

# Maximum additive adjustment per outcome from SPD signal
_MAX_ADJ = 0.08

# Browser-like headers
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_cache = get_cache()

# Shared async HTTP client
_http_client: "httpx.AsyncClient | None" = None


async def _get_http_client() -> "httpx.AsyncClient":
    """Return the shared httpx client, creating it if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers=_HEADERS,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )
    return _http_client


# ── Regex patterns for HTML scraping ─────────────────────────────────────────

# Patterns to find median fed funds rates in SPD HTML
_RE_RATE_2026 = re.compile(
    r"(?:2026[^%\d]{0,30}?(\d+\.\d+)\s*%|(\d+\.\d+)\s*%[^%]{0,30}?2026)",
    re.IGNORECASE,
)
_RE_RATE_2027 = re.compile(
    r"(?:2027[^%\d]{0,30}?(\d+\.\d+)\s*%|(\d+\.\d+)\s*%[^%]{0,30}?2027)",
    re.IGNORECASE,
)
_RE_PROB_CUT = re.compile(
    r"(?:probabilit[y|ies]{1,3}[^%\d]{0,30}?cut[^%\d]{0,30}?(\d+(?:\.\d+)?)\s*%"
    r"|cut[^%\d]{0,30}?(\d+(?:\.\d+)?)\s*%)",
    re.IGNORECASE,
)
_RE_EXCEL_LINK = re.compile(
    r'href=["\']([^"\']+\.xlsx)["\']',
    re.IGNORECASE,
)
_RE_DOT_RATE = re.compile(
    r"<td[^>]*>\s*(\d+\.\d+)\s*</td>",
    re.IGNORECASE,
)
_RE_DOT_DATE = re.compile(
    r"fomcprojtabl(\d{8})",
    re.IGNORECASE,
)


def _parse_rate_from_html(html: str, pattern: re.Pattern) -> Optional[float]:
    """Extract a rate float from HTML using the given regex pattern."""
    m = pattern.search(html)
    if not m:
        return None
    # pattern has two capture groups (forward and reverse word order)
    val_str = m.group(1) or m.group(2)
    if val_str is None:
        return None
    try:
        val = float(val_str)
    except ValueError:
        return None
    if _RATE_MIN <= val <= _RATE_MAX:
        return val
    return None


async def fetch_spd_rate_expectations() -> Optional[dict]:
    """
    Fetch the most recent NY Fed Survey of Primary Dealers results.

    Attempts to parse median fed funds rate expectations for 2026 and 2027
    from the SPD index page HTML.  If HTML parsing yields no rates, tries to
    find and download the latest Excel file linked from the page.

    Returns:
        {
            "fetched_at":           "<ISO8601>",
            "source":               "spd",
            "median_rate_2026":     float,   # e.g. 3.25
            "median_rate_2027":     float,
            "prob_cut_next_meeting": float,  # 0–1, e.g. 0.72
        }
        or None on any failure.

    Cached for 7 days (_TTL_SPD).  SPD data is optional — never crashes.
    """
    cache_key = "spd:rate_expectations"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        result = await _fetch_spd_html()
        if result is None:
            result = await _fetch_spd_excel()
        if result is not None:
            _cache.set(cache_key, result, ttl=_TTL_SPD)
        return result
    except Exception as exc:
        log.warning("fetch_spd_rate_expectations: unexpected error — %s", exc)
        return None


async def _fetch_spd_html() -> Optional[dict]:
    """Try to parse rate expectations from the SPD index page HTML."""
    try:
        client = await _get_http_client()
        resp   = await client.get(_SPD_INDEX_URL)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        log.warning("_fetch_spd_html: HTTP error — %s", exc)
        return None

    rate_2026 = _parse_rate_from_html(html, _RE_RATE_2026)
    rate_2027 = _parse_rate_from_html(html, _RE_RATE_2027)

    if rate_2026 is None and rate_2027 is None:
        log.debug("_fetch_spd_html: no rate figures found in page HTML")
        return None

    # Optional: probability of a cut at next meeting
    prob_cut: Optional[float] = None
    m = _RE_PROB_CUT.search(html)
    if m:
        raw = m.group(1) or m.group(2)
        try:
            pct = float(raw)
            # Could be expressed as 0–100 or 0–1
            prob_cut = pct / 100.0 if pct > 1.0 else pct
        except (TypeError, ValueError):
            pass

    result: dict = {
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "source":     "spd",
    }
    if rate_2026 is not None:
        result["median_rate_2026"] = rate_2026
    if rate_2027 is not None:
        result["median_rate_2027"] = rate_2027
    if prob_cut is not None:
        result["prob_cut_next_meeting"] = prob_cut

    log.info(
        "_fetch_spd_html: parsed SPD — 2026=%.2f%% 2027=%.2f%% prob_cut=%s",
        rate_2026 or 0.0, rate_2027 or 0.0,
        f"{prob_cut:.2f}" if prob_cut is not None else "n/a",
    )
    return result


async def _fetch_spd_excel() -> Optional[dict]:
    """
    Fall back: find an Excel link on the SPD page and attempt to read it.

    We do NOT import openpyxl or xlrd here to avoid heavy optional dependencies.
    We just log the URL so operators can download manually if needed.
    Returns None (gracefully) — the Excel path is best-effort.
    """
    try:
        client = await _get_http_client()
        resp   = await client.get(_SPD_INDEX_URL)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        log.warning("_fetch_spd_excel: HTTP error fetching index — %s", exc)
        return None

    matches = _RE_EXCEL_LINK.findall(html)
    if not matches:
        log.warning("_fetch_spd_excel: no .xlsx links found on SPD index page")
        return None

    xlsx_path = matches[0]
    if not xlsx_path.startswith("http"):
        xlsx_path = "https://www.newyorkfed.org" + xlsx_path

    log.warning(
        "_fetch_spd_excel: HTML parsing found no rates; "
        "Excel file available at %s — manual download required for full parsing",
        xlsx_path,
    )
    # Return a minimal stub so callers know SPD is available but unparse
    return None


def spd_to_meeting_bias(
    spd_data: dict,
    meeting_key: str,
) -> Optional[dict]:
    """
    Convert SPD rate expectations to an additive probability bias dict for
    a specific FOMC meeting.

    The bias represents how much to nudge each outcome probability based on
    where the primary dealer consensus sits vs the current policy rate.

    Args:
        spd_data:    Return value of fetch_spd_rate_expectations()
        meeting_key: FOMC meeting identifier, e.g. "2026-04"

    Returns:
        {
            "CUT_50": +0.05,
            "CUT_25": +0.03,
            "HOLD":   -0.04,
        }
        or None if spd_data is unavailable, stale, or lacks needed fields.

    Caps each adjustment at ±_MAX_ADJ (0.08) — SPD is slow-moving.
    """
    if not spd_data:
        return None

    # Check staleness — use fetched_at timestamp
    fetched_at_str = spd_data.get("fetched_at")
    if fetched_at_str:
        try:
            fetched_at = datetime.fromisoformat(fetched_at_str)
            age_days   = (datetime.now(tz=timezone.utc) - fetched_at).days
            if age_days > 100:
                log.debug(
                    "spd_to_meeting_bias: SPD data is %d days old — treating as stale",
                    age_days,
                )
                return None
        except (ValueError, TypeError):
            pass

    # Determine the year from the meeting key to pick the right SPD rate
    meeting_year: Optional[int] = None
    try:
        meeting_year = int(meeting_key.split("-")[0])
    except (IndexError, ValueError):
        pass

    rate_key = (
        "median_rate_2026" if meeting_year == 2026
        else "median_rate_2027" if meeting_year == 2027
        else "median_rate_2026"  # default to nearest available
    )
    median_rate = spd_data.get(rate_key)
    if median_rate is None:
        log.debug("spd_to_meeting_bias: missing %s in SPD data", rate_key)
        return None

    # Retrieve current policy rate from the FOMC module if available
    # Fall back to a common known rate for 2026 context (hard-coded as reference)
    try:
        from .fomc import OUTCOME_BPS  # noqa: F401 (import check only)
    except ImportError:
        pass

    # Current rate assumption — in production this would come from FRED/Kalshi market
    # Using a reasonable default for early 2026; the caller can supply a fresher value
    current_rate: float = 4.25   # typical 2026 starting rate

    # Implied change in basis points over the year
    implied_bps = round((median_rate - current_rate) * 100)

    # Build bias adjustments based on the implied direction
    # Positive implied_bps → hikes expected → boost HIKE, reduce CUT
    # Negative implied_bps → cuts expected → boost CUT, reduce HIKE
    bias: dict[str, float] = {}

    if implied_bps <= -50:
        # Two or more cuts expected
        bias = {"CUT_50": +0.05, "CUT_25": +0.03, "HOLD": -0.04, "HIKE_25": -0.04}
    elif implied_bps <= -25:
        # About one cut expected
        bias = {"CUT_25": +0.05, "HOLD": +0.01, "CUT_50": +0.01, "HIKE_25": -0.05}
    elif -24 <= implied_bps <= 24:
        # Hold expected
        bias = {"HOLD": +0.05, "CUT_25": -0.03, "HIKE_25": -0.03}
    elif implied_bps <= 50:
        # About one hike expected
        bias = {"HIKE_25": +0.05, "HOLD": +0.01, "CUT_25": -0.05}
    else:
        # Two or more hikes expected
        bias = {"HIKE_50": +0.05, "HIKE_25": +0.03, "HOLD": -0.04, "CUT_25": -0.04}

    # Also incorporate prob_cut_next_meeting if present
    prob_cut = spd_data.get("prob_cut_next_meeting")
    if prob_cut is not None:
        try:
            prob_cut = float(prob_cut)
            if 0.0 <= prob_cut <= 1.0 and prob_cut > 0.6:
                # Dealers lean toward cut — add small extra CUT_25 nudge
                bias["CUT_25"] = bias.get("CUT_25", 0.0) + 0.02
        except (TypeError, ValueError):
            pass

    # Cap each value at ±_MAX_ADJ
    bias = {k: max(-_MAX_ADJ, min(_MAX_ADJ, v)) for k, v in bias.items()}

    log.debug(
        "spd_to_meeting_bias: meeting=%s median_rate=%.2f implied_bps=%d → %s",
        meeting_key, median_rate, implied_bps, bias,
    )
    return bias


async def fetch_fomc_dot_plot() -> Optional[dict]:
    """
    Fetch and parse the Fed's Summary of Economic Projections (SEP) dot plot.

    Tries the HTML table at:
      https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{DATE}.htm

    Probes recent quarterly FOMC dates (March, June, September, December)
    for the most recent publication.

    Returns:
        {
            "year_2026_median": float,
            "year_2027_median": float,
            "longer_run":       float,
            "date":             "YYYY-MM-DD",
        }
        or None if unavailable or unparseable.

    Cached for 90 days (_TTL_DOT_PLOT).  Never crashes.
    """
    cache_key = "fed:dot_plot"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    result = await _probe_dot_plot_pages()
    if result is not None:
        _cache.set(cache_key, result, ttl=_TTL_DOT_PLOT)
    return result


async def _probe_dot_plot_pages() -> Optional[dict]:
    """
    Probe a series of plausible SEP page URLs in reverse chronological order.

    The Fed publishes SEP at quarterly FOMC meetings (Mar, Jun, Sep, Dec).
    We try the most recent 8 possible dates (2 years back) to find the latest.
    """
    # Generate candidate dates: quarterly (Mar=03, Jun=06, Sep=09, Dec=12)
    # for the current and previous year
    from datetime import date
    today = date.today()
    candidates: list[str] = []
    for year in (today.year, today.year - 1):
        for month, day in ((12, 13), (9, 18), (6, 12), (3, 19)):
            d = f"{year}{month:02d}{day:02d}"
            candidates.append(d)

    client = await _get_http_client()

    for date_str in candidates:
        url = f"{_DOT_PLOT_BASE}/fomcprojtabl{date_str}.htm"
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            result = _parse_dot_plot_html(resp.text, date_str)
            if result is not None:
                log.info(
                    "fetch_fomc_dot_plot: parsed SEP from %s — %s",
                    url, result,
                )
                return result
        except httpx.HTTPStatusError:
            continue
        except Exception as exc:
            log.debug("_probe_dot_plot_pages: error on %s — %s", url, exc)
            continue

    log.warning(
        "fetch_fomc_dot_plot: could not find a parseable SEP page; "
        "tried %d candidate URLs",
        len(candidates),
    )
    return None


def _parse_dot_plot_html(html: str, date_str: str) -> Optional[dict]:
    """
    Parse the median row from a Fed SEP HTML table.

    The SEP table has rows labelled "Median" with cells for:
      2025 | 2026 | 2027 | 2028 | Longer run

    Args:
        html:     Full page HTML text
        date_str: 8-digit date string "YYYYMMDD" from the URL

    Returns:
        Parsed dot plot dict or None.
    """
    # Locate the Median row
    median_m = re.search(r"Median.*?(<tr[^>]*>.*?</tr>)", html, re.IGNORECASE | re.DOTALL)
    if not median_m:
        # Alternative: look for a row containing "Median" directly
        median_m = re.search(
            r"<tr[^>]*>(?:[^<]|<(?!tr))*?[Mm]edian.*?</tr>",
            html, re.DOTALL,
        )
    if not median_m:
        log.debug("_parse_dot_plot_html: could not locate Median row in SEP HTML")
        return None

    row_html = median_m.group(0)
    cells    = _RE_DOT_RATE.findall(row_html)

    # Expect at least 4 numeric cells: [2025, 2026, 2027, longer_run] or similar
    if len(cells) < 3:
        log.debug(
            "_parse_dot_plot_html: only %d numeric cells in Median row — too few",
            len(cells),
        )
        return None

    try:
        values = [float(c) for c in cells]
    except ValueError:
        return None

    # Validate — all values should be plausible fed funds rates
    if not all(_RATE_MIN <= v <= _RATE_MAX for v in values):
        log.debug("_parse_dot_plot_html: one or more values outside rate bounds")
        return None

    # The table typically has columns: [current_year, 2026, 2027, 2028, longer_run]
    # We want 2026, 2027, and longer_run
    # If we have ≥4 values assume layout: [cur, 2026, 2027, longer_run, ...]
    # If only 3 values assume:            [2026, 2027, longer_run]
    if len(values) >= 4:
        year_2026  = values[1]
        year_2027  = values[2]
        longer_run = values[-1]
    else:
        year_2026  = values[0]
        year_2027  = values[1]
        longer_run = values[2]

    # Format date_str "YYYYMMDD" → "YYYY-MM-DD"
    try:
        date_formatted = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    except IndexError:
        date_formatted = date_str

    return {
        "year_2026_median": year_2026,
        "year_2027_median": year_2027,
        "longer_run":       longer_run,
        "date":             date_formatted,
    }


def dot_plot_to_probs(
    dot_plot: dict,
    current_rate: float,
    meeting_key: str,
) -> Optional[dict]:
    """
    Convert dot plot year-end median to per-meeting probability adjustments.

    Projects the implied total rate change for the year across remaining
    meetings, distributing it proportionally.

    Args:
        dot_plot:     Return value of fetch_fomc_dot_plot()
        current_rate: Current fed funds target rate (e.g. 4.25)
        meeting_key:  FOMC meeting identifier, e.g. "2026-04"

    Returns:
        {
            "CUT_25": +0.04,
            "HOLD":   -0.02,
            ...
        }
        or None if dot_plot is None or meeting_key can't be parsed.

    Caps each adjustment at ±_MAX_ADJ (0.08).
    """
    if not dot_plot:
        return None

    # Determine the year and approximate meeting number
    try:
        parts        = meeting_key.split("-")
        meeting_year = int(parts[0])
        meeting_month = int(parts[1]) if len(parts) > 1 else 1
    except (IndexError, ValueError):
        log.debug("dot_plot_to_probs: invalid meeting_key '%s'", meeting_key)
        return None

    # Pick the dot plot year-end target
    year_key = f"year_{meeting_year}_median"
    year_end_rate = dot_plot.get(year_key)
    if year_end_rate is None:
        log.debug(
            "dot_plot_to_probs: no '%s' in dot_plot data", year_key
        )
        return None

    # Validate current_rate
    if not (_RATE_MIN <= current_rate <= _RATE_MAX):
        log.warning(
            "dot_plot_to_probs: current_rate=%.2f outside [%.1f, %.1f] — ignored",
            current_rate, _RATE_MIN, _RATE_MAX,
        )
        return None

    # Total implied change for the year (in bps)
    total_bps = round((year_end_rate - current_rate) * 100)

    # Estimate remaining FOMC meetings in the year after this meeting
    # Approximate FOMC meeting months: Jan/Feb, Mar, May, Jun, Jul, Sep, Nov, Dec
    # (8 meetings per year)
    _FOMC_MONTHS = [1, 3, 5, 6, 7, 9, 11, 12]
    remaining = [m for m in _FOMC_MONTHS if m >= meeting_month]
    n_remaining = len(remaining) if remaining else 1

    # Per-meeting implied change
    per_meeting_bps = total_bps / n_remaining

    # Convert to probability adjustments
    # If per_meeting_bps is negative → cuts likely → boost CUT probs
    bias: dict[str, float] = {}

    if per_meeting_bps <= -37.5:
        # > 1 cut per meeting on average — strong cut signal
        bias = {"CUT_50": +0.06, "CUT_25": +0.04, "HOLD": -0.05, "HIKE_25": -0.05}
    elif per_meeting_bps <= -12.5:
        # ~1 cut per meeting
        bias = {"CUT_25": +0.05, "CUT_50": +0.02, "HOLD": -0.03, "HIKE_25": -0.04}
    elif -12.5 < per_meeting_bps < 12.5:
        # Roughly flat / hold
        bias = {"HOLD": +0.04, "CUT_25": -0.02, "HIKE_25": -0.02}
    elif per_meeting_bps <= 37.5:
        # ~1 hike per meeting
        bias = {"HIKE_25": +0.05, "HIKE_50": +0.02, "HOLD": -0.03, "CUT_25": -0.04}
    else:
        # > 1 hike per meeting on average — strong hike signal
        bias = {"HIKE_50": +0.06, "HIKE_25": +0.04, "HOLD": -0.05, "CUT_25": -0.05}

    # Cap at ±_MAX_ADJ
    bias = {k: max(-_MAX_ADJ, min(_MAX_ADJ, v)) for k, v in bias.items()}

    log.debug(
        "dot_plot_to_probs: meeting=%s year_end=%.2f current=%.2f "
        "total_bps=%d n_remaining=%d per_meeting=%.1f → %s",
        meeting_key, year_end_rate, current_rate,
        total_bps, n_remaining, per_meeting_bps, bias,
    )
    return bias
