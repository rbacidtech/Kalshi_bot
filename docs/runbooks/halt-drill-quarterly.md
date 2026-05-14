# Runbook: Quarterly halt drill

Phase 1.3 S.3.5 of `EdgePulse_Migration_Plan_2026.md`. Operator-driven exercise
that validates every halt path end-to-end. Documented gap per
`EdgePulse_Operating_S3_KillSwitches.md` §7: the bot has 5 auto-triggers and
several reactive cooldowns, but the halt pipeline (Redis → exec loop → operator
alert) has **never been drill-tested**.

Quarterly cadence catches drift before it matters in a real incident.

## Cadence

- **Frequency**: quarterly, first business day of each quarter (~Jan 2, Apr 1, Jul 1, Oct 1)
- **Duration**: 30 min — 1 hour
- **Owner**: operator
- **Pre-conditions**: bot is running, no genuine halt active, no positions
  pending exit at deadline-critical thresholds

## Drill targets

Rotate through all six on a 6-quarter cycle, or run all in one session if
time permits.

| # | Halt | Trigger | Cleared by |
|---|---|---|---|
| 1 | Soft halt (manual) | `redis-cli hset ep:config HALT_TRADING 1` | Operator `hset 0` |
| 2 | Drawdown breaker | Set `_start_portfolio_value` artificially high | UTC midnight auto-reset |
| 3 | Daily P&L loss (S.3.1) | Inject synthetic loss into `ep:performance:1` | Operator (HALT_TRADING + ep:performance:1 cleanup) |
| 4 | Balance velocity (S.3.2) | Force balance history with no executions | Operator (HALT_TRADING) |
| 5 | Per-strategy circuit breaker (S.3.3) | Inject negative P&L into `ep:performance:7.by_strategy` | Operator (remove strategy from disabled_model_sources + ep:auto_disabled) |
| 6 | Hard halt (S.3.4) | `touch /root/EdgePulse/.hard_halt` | Operator `rm /root/EdgePulse/.hard_halt` |

## Pre-drill checklist

```bash
# Confirm baseline
systemctl is-active edgepulse-intel edgepulse-exec edgepulse-api

# Confirm no halt currently active
redis-cli -s /run/redis/redis.sock hget ep:config HALT_TRADING
test -e /root/EdgePulse/.hard_halt && echo "HARD HALT EXISTS" || echo "no hard halt"
redis-cli -s /run/redis/redis.sock hgetall ep:halt

# Confirm no near-deadline positions
ep positions | awk -F'|' 'NR>1 {print $1, $NF}' | head -5
```

## Per-drill procedure

### Drill 1: soft halt (HALT_TRADING)

```bash
# TRIGGER
redis-cli -s /run/redis/redis.sock hset ep:config HALT_TRADING 1

# VALIDATE — within 60s, intel should log "HALT_TRADING set..." and stop publishing.
journalctl -u edgepulse-intel --since "2 min ago" | grep -i HALT_TRADING
journalctl -u edgepulse-exec  --since "2 min ago" | grep -i HALT_TRADING

# Confirm no new signals processed
redis-cli -s /run/redis/redis.sock xrevrange ep:executions + - COUNT 5

# CLEAR
redis-cli -s /run/redis/redis.sock hset ep:config HALT_TRADING 0
```

### Drill 2: drawdown breaker

Skipped — auto-clears at UTC midnight, hard to drill without setting up
synthetic portfolio_value movement. Validated indirectly when drill 3
fires (same `HALT_TRADING=1` end state).

### Drill 3: daily P&L loss (S.3.1)

```bash
# TRIGGER — inject -15% loss into ep:performance:1
# (assuming bankroll anchor today is set; check first)
NOW_DATE=$(date -u +%Y%m%d)
ANCHOR=$(redis-cli -s /run/redis/redis.sock get ep:bankroll_anchor:$NOW_DATE)
INJECT_LOSS=$((-1 * ANCHOR * 15 / 100))   # 15% loss → trips the 5% threshold

redis-cli -s /run/redis/redis.sock set ep:performance:1 \
  "{\"total_pnl_cents\": $INJECT_LOSS, \"total_trades\": 10, \"win_rate\": 0.2, \"by_strategy\": {}}"

# WAIT — _business_health_loop fires every 5 min. Expect halt within 6 min.

# VALIDATE
sleep 360
redis-cli -s /run/redis/redis.sock hget ep:config HALT_TRADING
redis-cli -s /run/redis/redis.sock hgetall ep:halt
# Expect reason=daily_pnl_loss, loss_pct ~15, threshold_pct ~5

# CLEAR
redis-cli -s /run/redis/redis.sock hset ep:config HALT_TRADING 0
redis-cli -s /run/redis/redis.sock del ep:performance:1   # let publisher republish naturally
```

### Drill 4: balance velocity (S.3.2)

Requires in-memory balance history with no recent executions. Hardest to
drill without disrupting normal operation. Suggested approach: stop intel
for 30 min so no signals fire, then synthesize a balance drop in Redis.

```bash
# Pause intel signal publishing (does not stop exec — balances continue to refresh)
systemctl stop edgepulse-intel
sleep 1800   # 30 min: wait for balance velocity window to accumulate

# Inject a synthetic balance drop in ep:balance
NODE_ID=intel-qvps-chi
CUR=$(redis-cli -s /run/redis/redis.sock hget ep:balance $NODE_ID | jq -r .balance_cents)
NEW=$(($CUR - $CUR * 20 / 100))
# (Full payload write — adapt jq to preserve schema)

# VALIDATE — within 5 min, _business_health_loop should trip:
sleep 360
redis-cli -s /run/redis/redis.sock hgetall ep:halt
# Expect reason=balance_velocity_unexplained

# CLEAR
redis-cli -s /run/redis/redis.sock hset ep:config HALT_TRADING 0
systemctl start edgepulse-intel
```

### Drill 5: per-strategy circuit breaker (S.3.3)

```bash
# TRIGGER — inject -15% bankroll loss attributed to a test strategy
ANCHOR=$(redis-cli -s /run/redis/redis.sock get ep:bankroll_anchor:$(date -u +%Y%m%d))
LOSS=$((-1 * ANCHOR * 15 / 100))
redis-cli -s /run/redis/redis.sock set ep:performance:7 \
  "{\"total_pnl_cents\": $LOSS, \"total_trades\": 5, \"win_rate\": 0.0,
    \"by_strategy\": {\"drill_test_strategy\": {\"trades\": 5, \"wins\": 0, \"pnl_cents\": $LOSS}}}"

# WAIT — _business_health_loop fires every 5 min
sleep 360

# VALIDATE
redis-cli -s /run/redis/redis.sock hget ep:config disabled_model_sources
# Expect drill_test_strategy to be present
redis-cli -s /run/redis/redis.sock hget ep:auto_disabled drill_test_strategy
# Expect ts_us=... pnl_cents=... threshold_pct=... window_days=7

# CLEAR
redis-cli -s /run/redis/redis.sock hset ep:config disabled_model_sources \
  "$(redis-cli -s /run/redis/redis.sock hget ep:config disabled_model_sources | sed 's/,drill_test_strategy//;s/drill_test_strategy,//;s/^drill_test_strategy$//')"
redis-cli -s /run/redis/redis.sock hdel ep:auto_disabled drill_test_strategy
redis-cli -s /run/redis/redis.sock del ep:performance:7
```

### Drill 6: hard halt (S.3.4)

```bash
# TRIGGER
touch /root/EdgePulse/.hard_halt

# VALIDATE — signal-consumer loop rejects new entries immediately:
redis-cli -s /run/redis/redis.sock xadd ep:signals '*' payload "$(...synthetic test signal...)"
journalctl -u edgepulse-exec --since "1 min ago" | grep "HARD HALT active"

# VALIDATE — _business_health_loop runs cancel-all within 5 min:
sleep 360
redis-cli -s /run/redis/redis.sock hgetall ep:halt
# Expect reason=hard_halt, cancelled_orders=N, flag_path=/root/EdgePulse/.hard_halt

# CLEAR — operator only; no Redis-side un-halt path exists by design
rm /root/EdgePulse/.hard_halt

# Confirm bot resumes normal signal processing:
sleep 30
journalctl -u edgepulse-exec --since "1 min ago" | grep -v "HARD HALT" | head
```

## Post-drill

For each drill that ran, record in `docs/incidents/DRILL_$(date -u +%Y%m%d).md`:
- Which drills ran
- For each: timestamp of trigger, observed halt latency, any anomalies
- Action items (if any halt path didn't fire correctly)

If any drill fails: file an incident, halt further drills until root cause
is fixed. Better to leave drills incomplete than to develop false confidence
in a broken halt path.

## Pre-conditions for next quarter

- All six halt paths drilled OR documented as skipped (with reason)
- Open action items from prior drills tracked to closure
- Bot stable for ≥7 days since the most recent drill

## Failure-mode notes

- **Drawdown breaker** (drill 2): hard to test in isolation. Validated indirectly
  via drill 3 (both end in `HALT_TRADING=1`).
- **Balance velocity** (drill 4): requires stopping intel — disruptive. Run only
  during a planned maintenance window or skip in tighter quarters.
- **Hard halt** (drill 6): the cancel-all step requires a kalshi_client; in
  paper-mode the cancel is a no-op (acceptable for the drill since the flag-file
  + ep:halt write are the primary validation targets).
