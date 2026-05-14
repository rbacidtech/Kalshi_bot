"""tests/test_correlation_caps.py — Engineering A.4 correlation caps unit tests."""

from __future__ import annotations

import asyncio

from ep_correlation_caps import classify, _resolve_caps, _DEFAULTS, _PREFIX_DAILY_DEFAULTS, _UNDERLYING_DAILY_DEFAULTS


def test_classify_known_prefixes():
    out = classify("KXNFLGAME-25NOV16-BUFKC-BUF")
    assert out["prefix"] == "KXNFLGAME"
    assert out["underlying"] == "NFL_SUNDAY"
    assert out["event_id"] == "KXNFLGAME-25NOV16-BUFKC"

    out = classify("KXMLBGAME-26MAY14-PHIATL-PHI")
    assert out["prefix"] == "KXMLBGAME"
    assert out["underlying"] == "MLB_DAY"

    out = classify("KXMVENFLSINGLEGAME-X-A")
    assert out["prefix"] == "KXMVENFLSINGLEGAME"  # longest match wins
    assert out["underlying"] == "NFL_SUNDAY"


def test_classify_crypto_and_weather():
    out = classify("KXBTCD-26MAY14-3000")
    assert out["underlying"] == "CRYPTO_DAILY"
    out = classify("KXHIGH-AUS-26MAY14-T95")
    assert out["underlying"] == "WEATHER_DAILY"


def test_classify_unknown_prefix_falls_back():
    out = classify("UNKNOWN-X")
    assert out["prefix"] == "UNKNOWN"
    assert out["underlying"] == "OTHER"


def test_classify_empty_ticker():
    out = classify("")
    assert out["prefix"] is None and out["event_id"] is None and out["underlying"] is None


def test_default_caps_sane():
    """Engineering A.4 numeric defaults match the digest."""
    assert _DEFAULTS["per_event_cap_cents"] == 15_000        # $150
    assert _DEFAULTS["total_daily_cap_cents"] == 120_000     # $1,200
    assert _DEFAULTS["downsize_minimum_cents"] == 1_000      # $10
    assert _PREFIX_DAILY_DEFAULTS["KXNFLGAME"] == 40_000     # $400
    assert _PREFIX_DAILY_DEFAULTS["KXBTCD"] == 25_000        # $250
    assert _UNDERLYING_DAILY_DEFAULTS["NFL_SUNDAY"] == 80_000  # $800


def test_resolve_caps_uses_overrides():
    overrides = {
        "per_prefix": {"KXNFLGAME": 50_000},
        "per_underlying": {"NFL_SUNDAY": 90_000},
    }
    caps = _resolve_caps(overrides, "KXNFLGAME", "NFL_SUNDAY")
    assert caps["per_prefix_cap_cents"] == 50_000
    assert caps["per_underlying_cap_cents"] == 90_000
    # Event + total stay at defaults
    assert caps["per_event_cap_cents"] == 15_000
    assert caps["total_daily_cap_cents"] == 120_000


def test_resolve_caps_unknown_prefix_falls_back():
    caps = _resolve_caps({"per_prefix": {}, "per_underlying": {}}, "FOOBAR", "MYSTERY")
    # Unknown prefix → 30_000 default; unknown underlying → 50_000 default
    assert caps["per_prefix_cap_cents"] == 30_000
    assert caps["per_underlying_cap_cents"] == 50_000


class _FakeRedis:
    """Minimal async Redis stand-in for testing check_candidate without a real bus."""
    def __init__(self, exposure: dict[str, int] | None = None, config: dict | None = None):
        self._exposure = exposure or {}
        self._config = config or {}
    async def hgetall(self, key):
        if "exposure" in key:
            return {k.encode(): str(v).encode() for k, v in self._exposure.items()}
        if key == "ep:config":
            return {k.encode(): str(v).encode() for k, v in self._config.items()}
        return {}
    async def hincrby(self, key, field, amount):
        if "exposure" in key:
            self._exposure[field] = self._exposure.get(field, 0) + amount
    async def expire(self, key, ttl):
        pass


def test_check_candidate_pass_through():
    """No prior exposure → request passes through unchanged."""
    from ep_correlation_caps import check_candidate
    r = _FakeRedis()
    res = asyncio.run(check_candidate(r, "KXMLBGAME-26MAY14-PHIATL-PHI", 5, 48))
    assert res["allowed_contracts"] == 5
    assert res["reason"] is None


def test_check_candidate_downsizes_at_event_cap():
    """Existing event exposure pushes cap binding to event level."""
    from ep_correlation_caps import check_candidate
    # $140 already on this event, $150 event cap → only $10 headroom
    r = _FakeRedis(exposure={
        "event:KXMLBGAME-26MAY14-PHIATL": 14_000,
    })
    # Request: 10 contracts × 48¢ = $4.80 — fits in headroom
    res = asyncio.run(check_candidate(r, "KXMLBGAME-26MAY14-PHIATL-PHI", 10, 48))
    assert res["allowed_contracts"] == 10
    # Request: 100 × 48¢ = $48 — exceeds $10 headroom; floor($10/$0.48) = 20 contracts
    res2 = asyncio.run(check_candidate(r, "KXMLBGAME-26MAY14-PHIATL-PHI", 100, 48))
    assert res2["allowed_contracts"] == 20
    assert res2["binding"] == "event"
    assert "DOWNSIZED_EVENT" in res2["reason"]


def test_check_candidate_rejects_below_minimum():
    """When headroom < $10 minimum, reject entirely."""
    from ep_correlation_caps import check_candidate
    r = _FakeRedis(exposure={
        "event:KXBTCD-26MAY14-3000": 14_950,   # leaves 50¢ headroom
    })
    res = asyncio.run(check_candidate(r, "KXBTCD-26MAY14-3000", 5, 30))
    assert res["allowed_contracts"] == 0
    assert "CORRELATION_CAP" in res["reason"]


def test_check_candidate_uses_overrides():
    """ep:config override_corr_cap_underlying_NFL_SUNDAY tightens the cap."""
    from ep_correlation_caps import check_candidate
    r = _FakeRedis(
        exposure={"underlying:NFL_SUNDAY": 19_000},   # $190 deployed
        config={"override_corr_cap_underlying_NFL_SUNDAY": "20000"},  # $200 cap
    )
    res = asyncio.run(check_candidate(r, "KXNFLGAME-25NOV16-BUFKC-BUF", 50, 50))
    assert res["allowed_contracts"] > 0
    assert res["allowed_contracts"] < 50
    assert res["binding"] in ("underlying", "event")
