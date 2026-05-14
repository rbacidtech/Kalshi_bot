"""Latency-optimized hot path — Engineering A.3.

Per Engineering A.3: **latency matters ONLY for H2H sum-to-1 arb** (~$23K/yr,
~50-200ms event lifetime). All other strategies (monotonicity, longshot,
prop yardage) trade on 6-12h windows with huge latency budgets.

Strict conventions for modules in `strategies.hot_path`:
  - No Pydantic, no dataclass introspection, no exception-based control flow
  - No structured logging (only plain log.debug if essential)
  - Pre-computed lookup tables (no per-call float math when avoidable)
  - No defensive copies / no dict re-building inside the inner loop
  - Each module is responsible for documenting its budget (e.g.,
    `BUDGET_MS_P95 = 5.0` constant) and verifying via local benchmark.

Cold-path (most scanners) lives in `kalshi_bot/strategy.py` and follows
normal Python idioms.

Hot-path budget breakdown for H2H (Engineering A.3 §):
  100ms p95 total = 5-15ms WS receipt + 1-5ms decision logic +
                    30-50ms POST round-trip + 10-20ms slack
"""
