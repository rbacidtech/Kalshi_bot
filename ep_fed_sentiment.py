"""
ep_fed_sentiment.py — LLM-based Fed speech sentiment scorer.

Fetches Fed governor speeches from the Fed's public RSS feed and scores them
hawkish/dovish using Claude. Returns a float from -1.0 (very dovish) to +1.0 (very hawkish).
Result is cached 6 hours in Redis under ep:fed_sentiment.
"""

import json
import logging
import os
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

log = logging.getLogger(__name__)

_FED_RSS_URL   = "https://www.federalreserve.gov/feeds/speeches.xml"
_REDIS_KEY     = "ep:fed_sentiment"
_CACHE_TTL_SEC = 6 * 3600   # 6 hours


def _fetch_recent_speeches(n: int = 3) -> str:
    """
    Fetch the Fed RSS feed and return a text block containing the titles
    and descriptions of the `n` most recent speeches.

    Uses only the stdlib (urllib + xml.etree.ElementTree) — no extra deps.
    Returns an empty string on any error.
    """
    try:
        req = urllib.request.Request(
            _FED_RSS_URL,
            headers={"User-Agent": "EdgePulse/1.0 (+https://github.com/edgepulse)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as exc:
        log.warning("Fed RSS fetch failed: %s", exc)
        return ""

    # RSS structure: <rss><channel><item>…</item>…</channel></rss>
    # The default namespace is empty for the Fed feed.
    channel = root.find("channel")
    if channel is None:
        log.warning("Fed RSS: no <channel> element found")
        return ""

    # Collect items with their publication date so we can filter out stale
    # speeches. Old speeches (>14 days) have no bearing on current Fed
    # stance and would bias the hawk/dove score toward yesterday's regime.
    # The previous behavior took the first N items regardless of date.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    from email.utils import parsedate_to_datetime as _pdt
    _MAX_SPEECH_AGE = _td(days=14)
    _now = _dt.now(_tz.utc)

    candidates: list[tuple[_dt, str, str]] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        desc  = (item.findtext("description") or "").strip()
        if not (title or desc):
            continue
        pub_raw = (item.findtext("pubDate") or "").strip()
        pub_dt: Optional[_dt] = None
        if pub_raw:
            try:
                pub_dt = _pdt(pub_raw)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=_tz.utc)
            except (TypeError, ValueError):
                pub_dt = None
        if pub_dt is not None and (_now - pub_dt) > _MAX_SPEECH_AGE:
            continue  # drop stale speeches
        candidates.append((pub_dt or _dt.min.replace(tzinfo=_tz.utc), title, desc))

    if not candidates:
        log.warning("Fed RSS: no recent (<14d) speeches found")
        return ""

    # Newest first; take up to n
    candidates.sort(key=lambda t: t[0], reverse=True)
    parts = [f"Title: {t}\nDescription: {d}" for _ts, t, d in candidates[:n]]
    return "\n\n".join(parts)


def _call_claude(text: str) -> Optional[float]:
    """
    Send `text` (speech summaries) to Claude and parse the returned JSON score.

    Returns a float in [-1.0, +1.0] or None on failure.
    """
    try:
        import anthropic
    except ImportError:
        log.error("ep_fed_sentiment: 'anthropic' package not installed — cannot score speeches")
        return None

    try:
        client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
        prompt = (
            "Rate the following recent Fed speeches on a hawkish-to-dovish scale. "
            'Return ONLY a JSON object: {"score": <float -1.0 to 1.0>, "reasoning": "<one sentence>"}. '
            "Score +1.0 = extremely hawkish (rate hikes expected), "
            "-1.0 = extremely dovish (rate cuts expected), 0.0 = neutral. "
            f"Speeches: {text}"
        )
        # Model read from env — avoid hardcoding a version that may be
        # deprecated in a future Anthropic API release. FED_SENTIMENT_MODEL
        # defaults to the current production alias; operator can override
        # without a code change on deprecation.
        _model = os.getenv("FED_SENTIMENT_MODEL", "claude-sonnet-4-6")
        message = client.messages.create(
            model=_model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present. Use regex so a single fence,
        # double fence, or the common ```json form all parse. Prior split()
        # approach broke on unbalanced fences and trailing whitespace.
        import re as _re
        _fence_match = _re.search(r"```(?:json)?\s*(.+?)\s*```", raw, _re.DOTALL)
        if _fence_match:
            raw = _fence_match.group(1).strip()
        elif raw.startswith("```"):
            # Fallback: unbalanced fence — take everything after the opener.
            raw = raw.split("```", 1)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        score = float(parsed["score"])
        reasoning = parsed.get("reasoning", "")
        if not (-1.0 <= score <= 1.0):
            log.warning("Fed sentiment score %.3f out of [-1, 1] — clamping", score)
            score = max(-1.0, min(1.0, score))
        log.info("Fed sentiment: score=%.3f  reasoning=%r", score, reasoning)
        return score
    except Exception as exc:
        log.warning("Fed sentiment Claude call failed: %s", exc)
        return None


async def get_fed_sentiment(redis_client) -> Optional[float]:
    """
    Return the current Fed speech sentiment score as a float in [-1.0, +1.0].

    Checks Redis cache (key ``ep:fed_sentiment``, TTL 6 h) first.  On a cache
    miss, fetches the three most recent Fed speeches via RSS, scores them with
    Claude, persists the result, and returns it.

    Returns ``None`` if both the cache and the live fetch fail.
    """
    # ── 1. Try cache ─────────────────────────────────────────────────────────
    try:
        cached = await redis_client.get(_REDIS_KEY)
        if cached is not None:
            score = float(cached)
            log.debug("Fed sentiment: cache hit — score=%.3f", score)
            return score
    except Exception as exc:
        log.debug("Fed sentiment: Redis read failed (%s) — fetching fresh", exc)

    # ── 2. Fetch fresh ────────────────────────────────────────────────────────
    text = _fetch_recent_speeches(n=3)
    if not text:
        log.warning("Fed sentiment: no speech text retrieved — skipping")
        return None

    score = _call_claude(text)
    if score is None:
        return None

    # ── 3. Cache result ───────────────────────────────────────────────────────
    try:
        await redis_client.set(_REDIS_KEY, str(score), ex=_CACHE_TTL_SEC)
        log.debug("Fed sentiment: cached score=%.3f for %dh", score, _CACHE_TTL_SEC // 3600)
    except Exception as exc:
        log.warning("Fed sentiment: Redis write failed (%s) — result not cached", exc)

    return score
