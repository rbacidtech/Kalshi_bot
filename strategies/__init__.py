"""EdgePulse strategy specifications and registries.

Phase 1.1 (S.4 Strategy Correctness) of EdgePulse_Migration_Plan_2026.md
introduces this package as the single source of truth for verdict-validated
strategy parameters. Imported at intel + exec service startup; mismatches
between the spec module and the verdict-doc total halt service startup.
"""

from .specs import (
    BOT_STRATEGIES,
    BotStrategyImpl,
    VERDICT_DOC_TOTAL_USD,
    VERDICT_STRATEGIES,
    VERDICT_TOLERANCE_USD,
    StrategySpec,
    verdict_doc_alignment_check,
)

__all__ = [
    "BOT_STRATEGIES",
    "BotStrategyImpl",
    "VERDICT_DOC_TOTAL_USD",
    "VERDICT_STRATEGIES",
    "VERDICT_TOLERANCE_USD",
    "StrategySpec",
    "verdict_doc_alignment_check",
]
