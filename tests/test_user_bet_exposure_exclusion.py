"""tests/test_user_bet_exposure_exclusion.py — guard for 443b0d5.

Pins the LONG_LIMIT / SHORT_LIMIT exposure calculation against silent
regression of the user_bet exclusion shipped in commit 443b0d5
("fix(exec): exclude user_bet positions from LONG_LIMIT/SHORT_LIMIT
exposure cap"). Without this test, the one-line `if p.get("user_bet"):
continue` is one refactor away from disappearing and re-creating the
2026-05-15 freeze: bot's operator-flagged personal bets eat the bot's
own safety budget, every signal LONG_LIMIT-rejected on a tight balance.

The function under test (``_compute_side_exposures``) is the pure
cost-basis-summing helper extracted from ``_process_signal`` in the
same commit so this behavior could actually be unit-tested. Anything
that breaks the user_bet skip — whether reverting the flag check,
inverting it, refactoring the loop into a comprehension that forgets
the flag — fires assertions here.
"""

from __future__ import annotations

import pytest

from ep_exec import _compute_side_exposures


def _pos(side: str = "yes", contracts: int = 1, entry_cents: int = 50,
         user_bet: bool | None = None, arb_id: str | None = None,
         contracts_filled: int | None = None) -> dict:
    """Build a minimal position record matching the shape in ep:positions."""
    p: dict = {"side": side, "contracts": contracts, "entry_cents": entry_cents}
    if contracts_filled is not None:
        p["contracts_filled"] = contracts_filled
    if user_bet is not None:
        p["user_bet"] = user_bet
    if arb_id is not None:
        p["arb_id"] = arb_id
    return p


# ── core regression guard: user_bet rows are excluded from both sides ──────

def test_user_bet_true_excluded_from_long():
    """A user_bet=True YES position contributes 0 to long exposure."""
    positions = {
        "BOT-1":  _pos(side="yes", contracts=10, entry_cents=30),  # 300¢ long
        "USER-1": _pos(side="yes", contracts=50, entry_cents=40, user_bet=True),
    }
    long_exp, short_exp = _compute_side_exposures(positions)
    assert long_exp == 10 * 30, (
        f"user_bet=True YES contaminated long exposure: got {long_exp}, expected 300"
    )
    assert short_exp == 0


def test_user_bet_true_excluded_from_short():
    """A user_bet=True NO position contributes 0 to short exposure."""
    positions = {
        "BOT-1":  _pos(side="no", contracts=10, entry_cents=70),   # 30c short cost × 10 = 300¢
        "USER-1": _pos(side="no", contracts=50, entry_cents=60, user_bet=True),
    }
    long_exp, short_exp = _compute_side_exposures(positions)
    assert short_exp == 10 * (100 - 70), (
        f"user_bet=True NO contaminated short exposure: got {short_exp}, expected 300"
    )
    assert long_exp == 0


def test_user_bet_absent_means_bot_managed():
    """Bot-placed positions leave user_bet absent (None). They MUST be counted.

    The codebase never writes user_bet=False — the only write site
    (ep_exec.py:_sync_positions_with_kalshi) sets it True; bot's own
    entry path leaves the key absent. So the exclusion must be
    truthy-keyed: `if p.get("user_bet")` skips True, counts None/False.
    """
    positions = {
        "BOT-1": _pos(side="yes", contracts=10, entry_cents=30),  # no flag set
    }
    long_exp, _ = _compute_side_exposures(positions)
    assert long_exp == 300, "absent user_bet must count (not skip)"


def test_user_bet_false_explicit_still_counts():
    """Defensive: if the flag ever IS set False explicitly, count the row."""
    positions = {
        "BOT-1": _pos(side="yes", contracts=10, entry_cents=30, user_bet=False),
    }
    long_exp, _ = _compute_side_exposures(positions)
    assert long_exp == 300


# ── orthogonal skips: arb_id remains independent of user_bet ───────────────

def test_arb_id_still_excluded():
    """arb_id positions are sized atomically; they were excluded before the
    user_bet patch and must remain excluded after it."""
    positions = {
        "ARB-A":  _pos(side="yes", contracts=5, entry_cents=50, arb_id="arb-123"),
        "ARB-B":  _pos(side="yes", contracts=5, entry_cents=50, arb_id="arb-123"),
    }
    long_exp, _ = _compute_side_exposures(positions)
    assert long_exp == 0


def test_arb_id_and_user_bet_both_excluded():
    """A position with both flags is still excluded (either flag is enough)."""
    positions = {
        "WEIRD": _pos(side="yes", contracts=100, entry_cents=50,
                      user_bet=True, arb_id="arb-9"),
        "BOT-1": _pos(side="yes", contracts=2, entry_cents=25),  # 50¢
    }
    long_exp, _ = _compute_side_exposures(positions)
    assert long_exp == 50


# ── the actual 2026-05-15 freeze topology, in miniature ────────────────────

def test_mixed_realistic_scenario_2026_05_15():
    """Reproduces the post-HDEL shape from the 2026-05-15 incident:
    16 user_bet=True positions consuming the long bucket plus 0
    bot-managed positions. Without the exclusion long_exp ≈ $71;
    with the exclusion long_exp == 0."""
    positions = {}
    # 8 KXFED user-bet positions, ~$0.50 cost basis each
    for i in range(8):
        positions[f"KXFED-26SEP-T{i}"] = _pos(
            side="yes", contracts=1, entry_cents=50, user_bet=True
        )
    # 4 KXNBAGAME user-bet positions, larger contract counts
    for i, (ct, ec) in enumerate([(50, 14), (50, 35), (75, 22), (80, 28)]):
        positions[f"KXNBAGAME-{i}"] = _pos(
            side="yes", contracts=ct, entry_cents=ec, user_bet=True
        )
    # 2 zero-contract tombstones with user_bet absent
    positions["KXFED-DEC-T1"] = _pos(side="yes", contracts=0, entry_cents=0)
    positions["KXFED-DEC-T2"] = _pos(side="yes", contracts=0, entry_cents=0)

    long_exp, short_exp = _compute_side_exposures(positions)
    assert long_exp == 0, (
        f"freeze reproduces: 16 user-bet positions still consuming "
        f"{long_exp/100:.2f}$ of long-cap budget"
    )
    assert short_exp == 0


# ── invariants that hold regardless of the flag ────────────────────────────

def test_empty_positions():
    long_exp, short_exp = _compute_side_exposures({})
    assert (long_exp, short_exp) == (0, 0)


def test_no_side_means_long():
    """Defensive default: position with neither side='no' nor side='yes' is
    treated as YES (long bucket). Matches the production loop's
    `if p.get("side") == "no": short else: long` semantics."""
    positions = {
        "X": {"contracts": 1, "entry_cents": 30},  # no 'side' key at all
    }
    long_exp, short_exp = _compute_side_exposures(positions)
    assert long_exp == 30 and short_exp == 0


def test_contracts_filled_preferred_over_contracts():
    """When both fields are set, contracts_filled wins. Matches
    `p.get('contracts_filled') or p.get('contracts', 1)` order."""
    positions = {
        "X": _pos(side="yes", contracts=10, contracts_filled=3, entry_cents=20),
    }
    long_exp, _ = _compute_side_exposures(positions)
    assert long_exp == 3 * 20  # uses contracts_filled, not contracts


def test_no_side_cost_basis_is_inverted_yes_price():
    """NO-side cost = (100 - entry_cents) per contract. entry_cents is
    always the YES-market price (SCHEMA.md invariant)."""
    positions = {
        "X": _pos(side="no", contracts=4, entry_cents=85),  # NO costs 15c, ×4 = 60
    }
    _, short_exp = _compute_side_exposures(positions)
    assert short_exp == 60


# ── ordering invariance ────────────────────────────────────────────────────

def test_dict_iteration_order_does_not_matter():
    """Whatever order we iterate the positions, totals are identical."""
    base = {
        "A": _pos(side="yes", contracts=10, entry_cents=30),
        "B": _pos(side="no",  contracts=20, entry_cents=70),
        "C": _pos(side="yes", contracts=5,  entry_cents=80, user_bet=True),
        "D": _pos(side="yes", contracts=2,  entry_cents=15),
    }
    long_a, short_a = _compute_side_exposures(base)
    # Re-insert in reverse order
    reversed_d = {k: base[k] for k in reversed(list(base.keys()))}
    long_b, short_b = _compute_side_exposures(reversed_d)
    assert (long_a, short_a) == (long_b, short_b)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
