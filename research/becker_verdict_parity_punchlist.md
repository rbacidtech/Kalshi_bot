# Becker verdict §3.1 per-strategy parity — open questions

This is a punch list for the verdict author of
`EdgePulse_Backtest_Verdict_2026.md`. The S.4 per-strategy parity harness
(`tests/test_per_strategy_parity.py` →
`scripts/backtest_h2h_sum_to_1.py`) cannot reproduce the §3.1 H2H
sum-to-1 table from the documented methodology. The remaining gap is a
verification blocker — not a tuning issue — and closing it requires
clarification on three specific points below.

## What we ran

Harness logic implements verdict §2.2 verbatim:

```sql
SELECT t.ticker, MEDIAN(t.yes_price/100.0) AS market_yes
FROM trades t JOIN markets m USING (ticker)
WHERE m.result IN ('yes','no')
  AND t.created_time < m.close_time
  AND t.created_time >= m.close_time - INTERVAL 6 HOUR
GROUP BY t.ticker
```

then groups legs by `event_ticker`, restricts to `n_legs == 2`, sums
the medians, counts events where `sum < 0.98`. Run against the full
67M-row resolved-trades parquet (`wealth_transfer/data/trades.parquet`,
built via `wealth_transfer/scripts/build_trades.py`).

Independent sanity checks: headline parity (taker −1.12%, maker +1.12%,
maker NO +1.28%, maker YES +0.77%) reproduces Becker's published numbers
exactly to four decimals. Pipeline is not the issue.

## What the harness produces

```
prefix         verdict (§3.1)  harness  ratio
KXMLBGAME              1112       488  0.44
KXMLSGAME               258         2  0.01  ← 3-leg, see Q3
KXWTAMATCH              654       111  0.17
KXATPMATCH              626       137  0.22
KXNCAAMBGAME            220        54  0.25
KXNCAAFGAME             302       129  0.43
KXNHLGAME               190        94  0.49
```

A separate run using "first sum-below-threshold in the [close-6h, close]
window via chronological walk of trade prints" produces 1.5–2.5× **over**:

```
KXMLBGAME    1112  →  2195   (1.97×)
KXWTAMATCH    654  →  1080   (1.65×)
KXATPMATCH    626  →  1121   (1.79×)
KXNCAAMBGAME  220  →   363   (1.65×)
KXNCAAFGAME   302  →   702   (2.32×)
KXNHLGAME     190  →   477   (2.51×)
```

**The verdict numbers sit between two methodologies the verdict
document seems to describe.**

## Hypotheses worth ruling in or out

### Q1. What snapshot statistic was actually used?

§2.2 specifies `MEDIAN(yes_price)` per leg. With pure median, harness
under-shoots by 2–6×. With chronological first-crossing on raw prints,
harness over-shoots by 1.5–2.5×. Candidates that land between:

- **Time-weighted average** of yes_price over the window (instead of
  median).
- **Median over a different anchoring** — e.g. `[close - 24h, close - 1h)`
  to exclude the final hour of settlement-contaminated trades.
- **k-th percentile other than 50** — 25th would over-shoot median
  (more arb events), 75th would under-shoot further; 25th is the
  plausible match.
- **"Any snapshot below threshold in window"** — i.e. count events
  where `min(yes_a + yes_b) < 0.98` rather than `median(yes_a) +
  median(yes_b) < 0.98`. Closer to first-crossing semantics but with
  an additional gate (depth-at-ask? fee-net edge?).
- **Median per leg, but only over trades after both legs have started
  trading** (instead of all trades in window).

Could you tell us which of these, or describe the actual statistic if
none match?

### Q2. Is the analysis script checked in anywhere?

Section 2.4 mentions `validate.py`. We can find references to `validate.py`
in `EdgePulse_Backtest_Verdict_2026.md` and the brief, but not the file
itself in `/root/research/`, `/root/EdgePulse/`, or
`/root/wealth_transfer/`. **The most direct way to close every open
question below is to check in the actual `validate.py` (or the script
that produced the §3.1 H2H sum-to-1 table specifically).** A unified
diff between what's there and what we implemented would let us reach
parity in one pass.

### Q3. Does §3.1 include 3-leg sum-to-1?

`KXMLSGAME` events on Kalshi list **three** outcomes (home win / away
win / draw): 313 resolved event_tickers, 948 distinct tickers, exactly
3 per event. The production scanner
`kalshi_bot.strategy.scan_h2h_sum_to_1_arb` skips every event with
`len(group) != 2`, so it never fires on soccer. Yet verdict §3.1 credits
KXMLSGAME with 258 arbs / $4,827 annual. Two possibilities:

- (a) §3.1 was computed with a more general k-leg sum-to-1 scanner that
  isn't in production. → The verdict overstates deployable P&L by the
  $4,827 MLS line and by every other 3-outcome league we'd theoretically
  add (EPL, La Liga, Serie A, Bundesliga, Ligue 1, UCL/UEL, World Cup).
- (b) §3.1 was computed with the production 2-leg scanner and the 258
  MLS arbs come from some other mechanism. → Something we're missing.

This is logged as a separate production-coverage gap in
`KNOWN_GAPS.md` (independent of the parity question) because the
correct response if (a) is to extend the scanner — that's real money on
the table — and the correct response if (b) is "we're confused, please
clarify."

### Q4. Subsampling — is it actually load-bearing?

`becker_benchmarks.json` describes the dataset as a *"stratified sample
of 67M-row resolved-trades materialization"* but `subsample.py` in
`EdgePulse_Backtest_DataPipeline.md` §3 does `DuckDB USING SAMPLE
5000000 ROWS` — uniform reservoir, **not stratified**, **no seed**.

We ran the harness against three independent 5M uniform subsamples;
MLB count moves by ≤3% vs the full 67M. Sampling alone cannot account
for the count drift.

So either:
- The "stratified" label in the JSON is incorrect (and §3.1 was
  computed against an essentially-uniform subsample, in which case Q1
  is the whole answer), or
- §3.1 was computed against a different, actually-stratified sample
  whose generator script lives somewhere we haven't found.

If the sample matters, it needs a deterministic seed and a documented
stratification scheme. If it doesn't, the JSON label should be
corrected.

## What's already ruled out

- **Pipeline correctness.** Headline parity (`tests/parity_test.py`)
  reproduces all four Becker headline numbers to 4 decimals on the
  full 67M-row dataset.
- **Coverage at the tour level.** Annualized event counts:
  `KXATPMATCH 3,005/yr`, `KXWTAMATCH 3,007/yr`. ATP/WTA main tours play
  ~2,500 main-draw singles matches per year; our data appears to include
  Challenger / qualifying matches too. We aren't missing events for
  tennis. The residual really is methodology, not scoping.
- **Both-legs / entry-exit double-count.** Audited the harness
  directly; one signal max per event_ticker by construction. Ratios
  drift 1.5–2.5× across sports (not literally 2.00 ± rounding), which
  by the deterministic-symmetry diagnostic is filter regime not
  counting regime.

## What we'd ask for, in priority order

1. **The actual analysis script** that produced the §3.1 H2H sum-to-1
   table. Closes everything in one pass.
2. **Clarify Q3** (3-leg coverage scope) — independent of parity, this
   gates a production scanner change.
3. **A deterministic-seeded sample** if subsampling is load-bearing,
   plus a corrected dataset label in `becker_benchmarks.json`. If
   subsampling isn't load-bearing, just correct the label.
4. **Confirm Q1** — which snapshot statistic actually produced §3.1.

Until at least item 1 lands (or 2+4 together), the six H2H parity
tests in `tests/test_per_strategy_parity.py` remain
`@pytest.mark.xfail(strict=True)`. They flip green the moment a
documented reproduction lands.
