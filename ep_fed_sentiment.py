"""
ep_fed_sentiment.py — LLM-based Fed speech sentiment scorer.

Fetches Fed governor speeches from the Fed's public RSS feed and scores them
hawkish/dovish using Claude. Returns a float from -1.0 (very dovish) to +1.0 (very hawkish).
Result is cached 6 hours in Redis under ep:fed_sentiment.
"""

import json
import logging
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

    items = channel.findall("item")[:n]
    if not items:
        log.warning("Fed RSS: no <item> elements found")
        return ""

    parts = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        desc  = (item.findtext("description") or "").strip()
        if title or desc:
            parts.append(f"Title: {title}\nDescription: {desc}")

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
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
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
