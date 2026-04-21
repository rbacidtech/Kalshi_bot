# EdgePulse Operations Runbook

## Services

| Service | Node | Command |
|---------|------|---------|
| Intel signal loop | Intel (167.71.27.43) | `systemctl restart edgepulse` |
| Exec order loop | Exec (172.93.213.88) | `ssh quantvps "systemctl restart edgepulse-exec"` |
| FastAPI dashboard | Intel | `systemctl restart edgepulse-api` |
| Redis+Postgres+Grafana | Intel | `docker compose restart` |

## First-time setup (Intel node)

```bash
# Install service files
cp edgepulse-intel.service /etc/systemd/system/
cp edgepulse-api.service   /etc/systemd/system/
systemctl daemon-reload
systemctl enable edgepulse edgepulse-api

# Log rotation
cp logrotate.conf /etc/logrotate.d/edgepulse

# Postgres backup cron (3am daily)
chmod +x backup_postgres.sh
echo "0 3 * * * /root/EdgePulse/backup_postgres.sh >> /root/EdgePulse/output/logs/backup.log 2>&1" | crontab -

# Health monitor cron (every 5 min)
chmod +x healthcheck.sh
echo "*/5 * * * * /root/EdgePulse/healthcheck.sh" | crontab -
```

## First-time setup (Exec node)

```bash
cp edgepulse-exec.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable edgepulse-exec
```

## Logs

```bash
journalctl -u edgepulse -f          # Intel (systemd)
tail -f output/logs/exec.log         # Exec (file)
docker compose logs -f redis         # Redis
```

## Emergency stop

```bash
systemctl stop edgepulse             # Intel
ssh quantvps "systemctl stop edgepulse-exec"  # Exec
# Set halt flag in Redis (stops new trades without killing process)
docker exec $(docker ps -q --filter name=redis) redis-cli -a PASSWORD hset ep:config HALT_TRADING 1
```
