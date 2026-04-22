# Runbook: Drawdown halt triggered

## Symptoms
- Metrics: `edgepulse_daily_pnl_pct{asset_class="kalshi"}` < -0.15
- `ep config | grep HALT` shows `HALT_TRADING    1`
- No new entries being placed; existing positions still exit normally

## Investigation
```bash
# Which positions moved against you?
ep positions

# Query losing exits today
PGPASSWORD=$(grep POSTGRES_PASSWORD /etc/edgepulse/edgepulse.env | cut -d= -f2)
PGPASSWORD=$PGPASSWORD psql -h 127.0.0.1 -U edgepulse -d edgepulse -c "
  SELECT ticker, side, status, reject_reason, fill_price, edge_captured,
         reported_at
  FROM executions
  WHERE reported_at > date_trunc('day', now())
  ORDER BY edge_captured ASC
  LIMIT 20;
"

# Is it one big loss or broad bleed?
# Broad bleed → data source / signal quality issue
# Single large loss → market event or model error
```

## Options

### Option A: Wait it out (recommended default)
- Halt auto-clears at 00:00 UTC
- Existing positions still exit normally
- No action needed unless positions are at further risk

### Option B: Resume early (only if root cause is confirmed fixed)
```bash
ep resume
# Monitor carefully for next 30 min
```

### Option C: Lower risk and resume
```bash
ep set llm_scale_factor 0.5   # half-size trades
ep resume
# Restores at 50% position sizing while allowing re-entry
```

## Post-incident
- `ep incident` — document losing trades and root cause
- Review signal quality for the losing asset class
- Consider whether Kelly fraction needs adjustment in llm_agent config
