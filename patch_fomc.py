import re

path = "/root/Kalshi_bot/kalshi_bot/models/fomc.py"
with open(path, "r") as f:
    content = f.read()

# 1. Add import os
content = content.replace("import httpx\n", "import os\nimport httpx\n", 1)

# 2. Insert FRED functions before the main public API section
fred_code = '''
# ── Source 4: FRED API fallback ───────────────────────────────────────────────
async def _fetch_fred_probs() -> tuple["dict[str, float] | None", "float | None"]:
    api_key = os.getenv("FRED_API_KEY", "")
    if not api_key:
        return None, None
    cache_key = "fred:probs"
    cached = _cache.get(cache_key)
    if cached:
        return cached.get("probs"), cached.get("rate")
    try:
        http = await _get_http_client()
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DFEDTARU&api_key={api_key}&file_type=json"
            "&sort_order=desc&limit=24"
        )
        resp = await http.get(url)
        resp.raise_for_status()
        obs = [o for o in resp.json().get("observations", []) if o.get("value") != "."]
        if not obs:
            return None, None
        current_rate = float(obs[0]["value"])
        rates = [float(o["value"]) for o in obs]
        changes = sum(1 for i in range(len(rates) - 1) if rates[i] != rates[i + 1])
        if changes == 0:
            probs = {"HOLD": 0.88, "CUT_25": 0.09, "CUT_50": 0.02, "HIKE_25": 0.01}
        elif rates[0] < rates[-1]:
            probs = {"HOLD": 0.60, "CUT_25": 0.30, "CUT_50": 0.07, "HIKE_25": 0.03}
        else:
            probs = {"HOLD": 0.70, "HIKE_25": 0.18, "CUT_25": 0.08, "CUT_50": 0.04}
        _cache.set(cache_key, {"probs": probs, "rate": current_rate}, ttl=3600)
        log.info("FRED: rate=%.2f%% probs=%s", current_rate, {k: f"{v:.0%}" for k, v in probs.items()})
        return probs, current_rate
    except Exception as exc:
        log.warning("FRED fetch failed: %s", exc)
        return None, None

async def _fetch_fred_fallback_meetings() -> "dict[str, MeetingProbs]":
    probs, rate = await _fetch_fred_probs()
    if not probs:
        log.warning("FRED fallback also failed — no meeting probs available.")
        return {}
    now = datetime.now(timezone.utc)
    result = {}
    dt = now.replace(day=1)
    for _ in range(8):
        from datetime import timedelta
        dt = (dt + timedelta(days=32)).replace(day=1)
        result[dt.strftime("%Y-%m")] = MeetingProbs(
            probs=probs, fetched_at=now, sources=["fred_model"], confidence=0.65
        )
    result[now.strftime("%Y-%m")] = MeetingProbs(
        probs=probs, fetched_at=now, sources=["fred_model"], confidence=0.65
    )
    log.info("FRED fallback: probs ready for %d meeting months", len(result))
    return result

'''

content = content.replace(
    "# ── Main public API ─────",
    fred_code + "# ── Main public API ─────",
    1
)

# 3. Patch fetch_fedwatch_all_meetings to use fallback
content = content.replace(
    "    if not data:\n        return {}",
    '    if not data:\n        log.info("CME unavailable — switching to FRED fallback.")\n        return await _fetch_fred_fallback_meetings()',
    1
)

with open(path, "w") as f:
    f.write(content)

print("✅ Patch applied successfully")
