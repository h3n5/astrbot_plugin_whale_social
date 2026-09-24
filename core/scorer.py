"""Local SpeakScore computation (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .gate import rate_in_window
from .topic import interest_multiplier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import PluginConfig
    from .models import GroupState


@dataclass
class ScoreBreakdown:
    topic_interest: float = 1.0
    activity_factor: float = 1.0
    social_energy: float = 1.0
    ignored_factor: float = 1.0
    consecutive_factor: float = 1.0
    total: float = 1.0
    matched_keywords: list[str] = field(default_factory=list)
    negative_hit: Optional[str] = None


def activity_factor(rate_60: int) -> float:
    if rate_60 > 20:
        return 0.35
    if rate_60 > 10:
        return 0.7
    if rate_60 <= 2:
        return 1.2
    return 1.0


def compute_score(
    state: "GroupState",
    text: str,
    config: "PluginConfig",
    now: float,
    *,
    umo: Optional[str] = None,
) -> ScoreBreakdown:
    interest, matched, negative = interest_multiplier(
        text,
        config.interest_keyword_list(umo),
        config.negative_keyword_list(),
        config.high_interest_bonus,
    )
    breakdown = ScoreBreakdown(
        topic_interest=interest,
        matched_keywords=matched,
        negative_hit=negative,
    )
    if negative is not None:
        breakdown.total = 0.0
        return breakdown

    breakdown.activity_factor = activity_factor(rate_in_window(state, now, 60))
    breakdown.social_energy = max(state.social_energy, 0.15)
    if state.last_bot_ignored:
        breakdown.ignored_factor = 0.35
    if state.consecutive_bot_messages > 0:
        breakdown.consecutive_factor = 0.3

    total = (
        breakdown.topic_interest
        * breakdown.activity_factor
        * breakdown.social_energy
        * breakdown.ignored_factor
        * breakdown.consecutive_factor
    )
    breakdown.total = max(0.0, min(total, 3.0))
    return breakdown
