# EdgePulse — Claude Code orientation

Distributed Kalshi trading bot, two services on a Chicago QuantVPS:
- `intel` (port 9091) — data fetch, model scoring, signal publish
- `exec` (port 8502 API) — signal consume, order placement, exit management

Communication: Redis streams + hashes via Unix socket. Live trading; treat the production state with care.

## Read these before substantive work

- `README.md` — high-level + quickstart
- `SYSTEM_OVERVIEW.md` — architecture, services, data flow
- `SCHEMA.md` — authoritative Redis / data-shape reference; read before touching positions or signals
- `KNOWN_GAPS.md` — open issues and deferred work (transient state lives here, not in this file)
- `DEPLOY.md` — deploy.sh contract and rollback procedure

## Hard invariants (true throughout the codebase)

### `entry_cents` is ALWAYS the YES-market price × 100
For both yes AND no positions. Never store the raw NO price.
- YES position cost per contract = `entry_cents`
- NO position cost per contract = `100 - entry_cents`
- P&L move (works for both sides): `move_cents = entry_cents - current_yes_price_cents`

### Redis is on a Unix socket in production
- `REDIS_URL=unix:///run/redis/redis.sock`
- CLI: `redis-cli -s /run/redis/redis.sock ...`
- TCP `localhost:6379` exists but is reserved for the read-only MCP user; production services use the socket.

### `.env` lives in two places — keep them in sync
- `/root/EdgePulse/.env` — authoritative copy. Gitignored. Never `git add`.
- `/etc/edgepulse/edgepulse.env` — systemd `EnvironmentFile=`.
- Verify: `diff /root/EdgePulse/.env /etc/edgepulse/edgepulse.env`

### `NODE_ID` and `MODE` are set in systemd units, NOT in .env
Each service's `.service` file sets its own `Environment=NODE_ID=... MODE=...`. Adding them to `.env` would let EnvironmentFile overwrite the per-service values. Current values:
- intel: `NODE_ID=intel-qvps-chi MODE=intel`
- exec: `NODE_ID=exec-qvps-chi MODE=exec`

### `ep:config` Redis hash overrides everything in .env at runtime
The advisor and operator write `override_edge_threshold`, `override_min_confidence`, `llm_kelly_fraction`, `HALT_TRADING`, etc. to `ep:config`. These are read on every signal evaluation and take precedence over .env. **Always check `ep:config` first** when investigating "why aren't signals firing" or "why is sizing weird":
```bash
redis-cli -s /run/redis/redis.sock hgetall ep:config
```

### Import order: `ep_config` before `kalshi_bot.*`
`ep_config.py` does `sys.path.insert(0, str(_here))` to make `kalshi_bot` importable. Any `ep_*.py` that uses `kalshi_bot` must `import ep_config` (or `from ep_config import ...`) first.

### Two distinct config systems — do not conflate
- `ep_config.py` — distributed-system config (NODE_ID, MODE, REDIS_URL, stream names, EP_SIGNALS, etc.)
- `kalshi_bot/config.py` — trading config (EDGE_THRESHOLD, KELLY_FRACTION, MAX_CONTRACTS, etc.)

Both read from the same `.env`. They are separate objects with separate concerns.

## Workflow rules

### Always commit AND push before `./deploy.sh`
`deploy.sh` blocks on dirty tree. The Claude Code `deploy-preflight` hook also blocks on unpushed commits. `--force` is for emergencies only and only when the user explicitly asks.

### `edge_threshold` is a literal fee-adjusted EV floor
As of 2026-04-24, no hidden 0.7 multiplier. The value (env or `ep:config:override_edge_threshold`) means what it says: the minimum fee-adjusted EV per contract in decimal dollars. Pre-2026-04-24 code multiplied by 0.7 silently — if you see that pattern in old commits, it's gone for a reason (KNOWN_GAPS.md Audit #5).

### Editing `kalshi_bot/models/fomc.py` — use Python, not Edit
The file contains em-dashes (—), arrows (→), and Unicode minus signs (−) that the Edit tool's exact-match silently fails on. Use:
```python
content = open('/root/EdgePulse/kalshi_bot/models/fomc.py').read()
content = content.replace(old, new, 1)
open('/root/EdgePulse/kalshi_bot/models/fomc.py', 'w').write(content)
```
Verify after: `python3 -c "import ast; ast.parse(open('/root/EdgePulse/kalshi_bot/models/fomc.py').read()); print('OK')"`

### Log format is JSONL
`/root/EdgePulse/output/logs/kalshi_bot.jsonl` — one JSON object per line. Plain grep returns blobs; parse with python.

## Trading-relevant traps

### `meeting` field must be populated for the per-meeting cap to work
The meeting concentration gate uses `p.get("meeting") != sig.meeting`, which treats empty string as non-matching — so positions with `meeting=""` silently bypass `MAX_POSITIONS_PER_MEETING=4`. If a meeting ever shows >4 positions, check the `meeting` field on each.

### `min_confidence` is honored (was silently ignored pre-2026-04-24)
`kalshi_bot/strategy.py` previously hardcoded `s.confidence >= 0.50` instead of the parameter. Fixed. To verify it still works, set `override_min_confidence=0.99` and confirm signal volume drops.

### Arb signals bypass the YES-price gate
`_PRICE_GATE_EXEMPT_SOURCES` in `strategy.py` exempts these from `MIN_YES_ENTRY_PRICE`:
`fomc_butterfly_arb`, `monotonicity_arb`, `calendar_spread_arb`, `gdp_fomc_coherence`, `cross_series_coherence`.

### Permanently-dead data sources (do not try to revive)
- ZQ 30-day Fed Funds futures — CME WAF, 403 since 2024
- SOFR SR3 (Yahoo) — delisted ~2024, 404
- SOFR SR3 (CME API) — 404 on our subscription scope
- Binance — 451 from US VPS, geo-blocked
T-bill term structure (FRED DTB3/DTB6/DTB1YR) replaced ZQ on 2026-04-23.

## Quick references

### Key Redis keys
```
ep:signals          STREAM    Intel → Exec signals
ep:executions       STREAM    Exec → Intel fill reports
ep:positions        HASH      ticker → position JSON
ep:prices           HASH      ticker → price snapshot
ep:balance          HASH      node_id → balance JSON
ep:config           HASH      runtime overrides (HALT_TRADING, llm_*, override_*)
ep:health           HASH      data source health registry
```

### Common Redis ops
```bash
SOCK=/run/redis/redis.sock
redis-cli -s $SOCK hgetall ep:balance         # balance per node
redis-cli -s $SOCK hgetall ep:config          # runtime overrides
redis-cli -s $SOCK hlen ep:positions          # open position count
redis-cli -s $SOCK hkeys ep:positions         # all tickers
redis-cli -s $SOCK hgetall ep:health          # data source liveness

# Emergency halt
redis-cli -s $SOCK hset ep:config HALT_TRADING "1"
```

### Service control
```bash
systemctl status edgepulse-intel edgepulse-exec edgepulse-api
journalctl -u edgepulse-intel --since "10 min ago"
```

### Deploy
```bash
cd /root/EdgePulse
git status                                    # must be clean
git push                                      # must be in sync with origin
./scripts/deploy.sh                           # or --intel | --exec
```

## Claude Code harness in this environment

Already configured in `/root/.claude/`:
- `hooks/deploy-preflight.sh` — blocks `./deploy.sh` on dirty tree or unpushed commits
- `hooks/python-edit-check.sh` — AST-parses edited `.py` files; runs the test if file is `tests/test_*.py`
- `commands/scanner-audit.md` — `/scanner-audit` slash command for data-source liveness probe
- MCP `redis` (in `~/.claude.json`) — read-only access via `claude-mcp` ACL user

## When in doubt

1. If a memory entry conflicts with current code, trust the code.
2. If `KNOWN_GAPS.md` and a memory disagree on what's broken, run `/scanner-audit` for ground truth.
3. Never commit `.env`, `coinbase_key.pem`, or anything in `/root/EdgePulse/output/`.
4. The user runs this on a live trading account. Default to caution on Redis writes, deploy, and systemctl restart.
