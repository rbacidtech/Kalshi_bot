# Runbook: Exec service crashed

## Symptoms
- `ep status` shows edgepulse-exec failed
- Positions exist in ep:positions but no exit checks running

## Immediate action
1. `journalctl -u edgepulse-exec -n 200` — identify crash reason
2. Check memory: `free -h` and `dmesg | grep -i oom`
3. If OOM, check which service blew up memory

## Recovery

### Case A: simple crash, restart
```bash
systemctl restart edgepulse-exec
sleep 30
ep status
```

### Case B: crash loop (crashes within 10s of start)
1. Halt trading immediately: `ep halt`
2. Check logs for the root cause
3. Common causes:
   - Redis unreachable → `redis-cli -s /run/redis/redis.sock ping`
   - Kalshi API auth failure → verify key file `chmod 600 /root/EdgePulse/private_key.pem`
   - Postgres schema mismatch → `psql -h 127.0.0.1 -U edgepulse -d edgepulse -c '\dt'`
   - Config parse error → `cat /etc/edgepulse/edgepulse.env`
4. Fix root cause
5. `systemctl start edgepulse-exec`
6. `ep resume`

### Case C: Exec is wedged (running but not processing)
1. `ep health` — check which subsystem is failing
2. `systemctl restart edgepulse-exec`
3. Verify: `ep status`

## Post-incident
- `ep incident` — open today's incident note
- If root cause was a bug, file a fix
- If it was an external failure, update this runbook
