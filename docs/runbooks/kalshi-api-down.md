# Runbook: Kalshi API not responding

## Symptoms
- Circuit breaker open on Kalshi executor
- All orders failing with HTTP_ERROR or 5xx
- Metrics: `edgepulse_circuit_breaker_state{breaker="kalshi"}` == 2

## Diagnosis
```bash
# Check API reachability (no auth required)
curl -sI https://api.elections.kalshi.com/trade-api/v2/exchange/status

# Check circuit breaker state in health endpoint
ep health
```

## Action

### Case A: Kalshi is down for everyone
- Trading is automatically halted by circuit breaker — leave it
- Existing positions still exist; Kalshi resolves markets server-side
- Monitor for restoration
- Circuit breaker auto-closes after a successful probe — no manual action needed

### Case B: Only our auth is failing
1. Check key hasn't been invalidated (check Kalshi dashboard)
2. Verify system clock accuracy — RSA signing requires a fresh timestamp:
   ```bash
   timedatectl status
   ```
3. Verify key file permissions:
   ```bash
   ls -la /root/EdgePulse/private_key.pem  # must be 600
   ```
4. If key is fine, restart exec to force a fresh auth:
   ```bash
   systemctl restart edgepulse-exec
   ```

## Positions at risk during outage
- Near-expiry positions that need pre-expiry exits → will auto-resolve server-side
- Positions with stops near trigger → may breach without protection
- Nothing actionable during a full outage; monitor and document

## Post-incident
- `ep incident`
- Note duration and any position P&L impact
