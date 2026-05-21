# EdgePulse — DECISIONS log

Append-only record of design decisions where the rationale isn't obvious from
the code or commit message alone. Each entry: what was decided, what was
considered and rejected, why. Keep entries dated + linkable from commit
messages.

**Entry template** (use as the structure for new decisions):
1. **Problem** — what was observed, in concrete terms
2. **Diagnosis evidence** — what was tested + what alternative hypotheses were ruled out (must be falsifiable, not "we think")
3. **Options considered** — table of A/B/C/D with what each is + why rejected
4. **Decision rationale** — why the chosen option, including any scope claims
5. **Empirical validation** — where the scope or threshold was confirmed against observed reality, not just code citations
6. **Implementation sketch** — if not yet written, the planned shape
7. **Test plan** — including which existing gates (parity, deploy, etc.) do and DON'T cover this
8. **Why not now / urgency** — only if there's a reason to defer
9. **Cross-references** — commits, log paths, conversation timestamps

---

## 2026-05-21 — WS cold-start subscribe race: fix at `_on_open` (Option A)

### Problem

`kalshi_bot/websocket.py:_on_open` connects to Kalshi WS but only sends
subscribe frames if `self._tickers` is non-empty at handshake time. The
constructor at `ep_intel.py:1429` (`ws = KalshiWebSocket(state=state, auth=auth, paper=ws_paper)`)
doesn't pass `tickers=` — list starts empty. Market discovery later in
the intel boot calls `ws.subscribe_tickers(...)`, which DOES send frames,
but only after ~20 minutes of cold-start work.

Result: WS reports "connected" immediately, but produces no price data
for ~20 minutes until subscribes go out. Health checker
(`ep_health._record`) flags `kalshi_ws DOWN (critical)` every 2 min,
failures counter accumulates (observed: failures=10 by 13:09 UTC on
2026-05-21).

### Diagnosis evidence

JSONL log span 2026-05-21 12:51:22 → 13:11:28:
- Outbound `Subscribed to ...` debug events during gap: **0**
- First subscribe frame: 13:11:28.124 (948 frames in 365 ms)
- Inbound Kalshi messages during gap: 0
- Conclusion: subscribe frame **never sent** during gap (not "slow snapshot")

### Considered alternatives

| Option | Description | Why not chosen |
|---|---|---|
| **A (chosen)** | At connect, fetch open markets for our trading-prefix list via REST, then send subscribes immediately. | Real fix. Touches ingestion only — no business-logic risk. One REST call to populate. |
| B | Extend `ep_health.kalshi_ws` cold-start grace window to ~30 min so failures don't accumulate during the gap. | Cosmetic. Hides the alert but doesn't recover the 20-min data gap. Phase 2 longshot strategies that need recent trade history would still miss the first window. Duct tape. |
| C | Move `KalshiWebSocket` instantiation to AFTER market discovery in `ep_intel`. | Reorders intel boot sequence — touches non-ingestion code, more regression surface than A. |
| D | Subscribe to "all open markets" with no prefix filter. | Wasteful WS bandwidth + may bump subscription limits. Rejected (per operator). |

### Subscribe-scope decision

Filter to series the bot actually trades. Source of truth in code:

- `strategies/specs.py` `VERDICT_STRATEGIES`: KXMLBGAME, KXMLSGAME, KXWTAMATCH,
  KXATPMATCH, KXNCAA{MB,F}GAME, KXNHLGAME, KXNFLRSHYDS, KXNFLRECYDS,
  KXBTCD, KXETHD, KXMVE\* (4 prefixes), KXHIGH\* (city variants),
  KXTRUMP\*, SECPRESS, VANCEMENTION, APRPOTUS, 538APPROVE, KXFED-
- `kalshi_bot/strategy_phase2.py`: KXNCAA{MB,F}SPREAD, KX{NHL,NBA,NFL}SPREAD,
  KXNCAA{F,MB}TOTAL, KX{NBA,NFL}TOTAL, EURUSD, USDJPY, WTI, TNOTED
- `BOT_STRATEGIES` active scanners: KXFED, KXGDP

~35 distinct prefixes. **Not "all open markets"** — that would be wasteful
WS bandwidth and risk hitting Kalshi subscription limits.

### Empirical validation of the scope claim

The 35-prefix list is a code citation. To verify it matches reality (open
markets within these prefixes that actually exist on Kalshi today), checked
against the post-market-discovery subscribe batch that fired at 2026-05-21
13:11:28 UTC:

- Frames sent in the batch: **948**
- Channels per ticker: **2** (`orderbook_delta` + `trade`)
- Unique tickers: **474**
- Range of prefixes covered: matches the 35-prefix code list above
- Excluded prefixes that exist on Kalshi but the bot doesn't trade (e.g.,
  KXNCAAGAME-women's-tennis-variants, KXENTAWARD\*, KXSPACE\*): not in the
  batch → not in scope → correct

In other words: when the bot finally got its act together at 13:11 today, it
subscribed to exactly the markets it should subscribe to. The fix only needs
to move that batch from "20 minutes after connect" to "at connect." Scope is
not what needs solving.

### Implementation sketch (not yet written)

1. New helper in `kalshi_bot/websocket.py` or a separate module: `fetch_initial_tickers(client, prefix_list)` — paginated `GET /markets?status=open&series_ticker=<prefix>` per prefix, deduplicate.
2. Constructor or `start()` accepts a `prefix_list` argument; defaults to the empty list (backward-compatible).
3. `_on_open` change: if `self._tickers` empty AND `self._prefix_list` non-empty, call the fetcher synchronously, populate `self._tickers`, then `_send_subscriptions`.
4. `ep_intel.py:1429` instantiation: pass `prefix_list=` derived from
   `strategies.specs.VERDICT_STRATEGIES` + `BOT_STRATEGIES` ticker_prefixes,
   merged with `kalshi_bot.strategy_phase2` constants.

### Test plan

This is **ingestion-layer** — does NOT route through S.4 parity gate
(which validates verdict-spec correctness, not WS subscribe correctness).
Parity harness assumes warm cache. Don't claim parity-test coverage in
the commit.

Required tests:
1. Unit test: mock `KalshiWebSocket._ws.send`, instantiate with a
   prefix_list, call `_on_open(mock_ws)`, assert N×2 send calls (×N
   tickers, ×2 channels per ticker).
2. Smoke test on the cold-start path: start intel, watch JSONL for
   "Subscribed to" debug events within 30s of connect.

### Why not now

Not blocking. Bot self-recovers in ~20 min on each fresh boot. Operator
just witnessed the cycle (2026-05-21 12:51 → 13:11) and the data is
flowing now. The 20-min false-positive `kalshi_ws DOWN` health alert
during cold-start is the visible cost; trading impact is "Phase 2
longshot strategies needing median-snapshot data miss the first 20-min
window after each reboot."

Reboots are rare (last one 2026-05-21, before that 2026-04-15). Fix
when convenient; not urgent.

### Cross-references

- Operator-side conversation: 2026-05-21 ~13:20 UTC
- Confirmed by: bot operator
- Diagnosis script: `python3 ... /root/EdgePulse/output/logs/kalshi_bot.jsonl`
  parsing for SUB-SENT vs INBOUND events between 12:51 and 13:12

