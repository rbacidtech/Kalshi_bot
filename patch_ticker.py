path = "/root/Kalshi_bot/kalshi_bot/models/fomc.py"
with open(path, "r") as f:
    content = f.read()

new_parse = '''
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
            current_rate = 4.33  # midpoint; FRED will refine this
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

'''

# Replace the existing parse_fomc_ticker function
old_start = "def parse_fomc_ticker(ticker: str) -> dict | None:"
old_end   = "# ── Main public API ─────"

start_idx = content.find(old_start)
end_idx   = content.find(old_end)
if start_idx == -1 or end_idx == -1:
    print("❌ Could not find parse_fomc_ticker — check file manually")
else:
    content = content[:start_idx] + new_parse + "\n" + content[end_idx:]
    with open(path, "w") as f:
        f.write(content)
    print("✅ parse_fomc_ticker patched successfully")
