"""Social-factor scoring (no LLM, no AstrBot).

The speak probability is a product of named social factors instead of a
keyword score. Keywords only feed *relevance* (recall): they can raise the
chance of consulting the decision model, but their absence no longer mutes
the bot. The decision itself stays with the LLM stage.

Factor contract (the seam for future strategies):

- addressed_to_me          the trigger message talks to the bot (quote/reply)
- conversation_relevance   thread-topic relevance; keyword hits raise it
- conversation_activity    how busy the room is right now (dense chatter = no)
- social_opportunity       is this a natural moment to interject (question,
                           calm moment)
- recent_reply_frequency   has the bot been talking too much lately
- social_energy            send battery (drains per send, refills daily)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .gate import rate_in_window
from .topic import keyword_hits, negative_hit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import PluginConfig
    from .models import GroupState

QUESTION_TOKENS = ("？", "?", "吗", "呢", "么", "怎么", "为什么", "如何", "求", "推荐", "有没有", "谁知道", "哪个")

RELEVANCE_HIT_BONUS = 0.35
ACTIVITY_CALM = 1.2
ACTIVITY_NORMAL = 1.0
ACTIVITY_BUSY = 0.6
ACTIVITY_FLOODED = 0.3
OPPORTUNITY_QUESTION_BONUS = 0.35
OPPORTUNITY_CALM_BONUS = 0.15
OPPORTUNITY_CAP = 1.5
ADDRESSED_FACTOR = 2.0
FREQUENCY_WINDOW_SECONDS = 300.0
FREQUENCY_LONG_WINDOW_SECONDS = 1800.0
FREQUENCY_LONG_LIMIT = 6
FREQUENCY_FACTORS = {0: 1.0, 1: 0.6, 2: 0.3}
FREQUENCY_HEAVY = 0.1
ENERGY_FACTOR_FLOOR = 0.15
SCORE_CAP = 3.0


@dataclass
class SocialFactors:
    addressed_to_me: float = 1.0
    conversation_relevance: float = 1.0
    conversation_activity: float = 1.0
    social_opportunity: float = 1.0
    recent_reply_frequency: float = 1.0
    social_energy: float = 1.0
    total: float = 1.0
    matched_keywords: list[str] = field(default_factory=list)
    negative_hit: Optional[str] = None
    question_like: bool = False
    quiet_gap_seconds: float = 0.0


def activity_factor(rate_60: int) -> float:
    if rate_60 > 20:
        return ACTIVITY_FLOODED
    if rate_60 > 10:
        return ACTIVITY_BUSY
    if rate_60 <= 2:
        return ACTIVITY_CALM
    return ACTIVITY_NORMAL


def is_question_like(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    return any(token in value for token in QUESTION_TOKENS)


def _relevance_factor(text: str, keywords: list[str], bonus: float) -> tuple[float, list[str]]:
    hits = keyword_hits(text, keywords)
    if not hits:
        return 1.0, []
    return min(1.0 + RELEVANCE_HIT_BONUS * len(hits), bonus), hits


def _opportunity_factor(state: "GroupState", text: str, now: float) -> tuple[float, bool, float]:
    question = is_question_like(text)
    gap = now - state.last_user_message_time if state.last_user_message_time else 0.0
    calm = rate_in_window(state, now, 60) <= 3
    factor = 1.0
    if question:
        factor += OPPORTUNITY_QUESTION_BONUS
    if calm:
        factor += OPPORTUNITY_CALM_BONUS
    return min(factor, OPPORTUNITY_CAP), question, gap


def _frequency_factor(state: "GroupState", now: float) -> float:
    recent = sum(
        1
        for stamp in state.proactive_send_times
        if now - stamp <= FREQUENCY_WINDOW_SECONDS
    )
    long_term = sum(
        1
        for stamp in state.proactive_send_times
        if now - stamp <= FREQUENCY_LONG_WINDOW_SECONDS
    )
    factor = FREQUENCY_FACTORS.get(recent, FREQUENCY_HEAVY)
    if long_term >= FREQUENCY_LONG_LIMIT:
        factor = min(factor, FREQUENCY_HEAVY)
    return factor


def compute_score(
    state: "GroupState",
    scored_text: str,
    config: "PluginConfig",
    now: float,
    *,
    umo: Optional[str] = None,
    addressed: bool = False,
    trigger_text: str = "",
) -> SocialFactors:
    interest, matched = _relevance_factor(
        scored_text,
        config.interest_keyword_list(umo),
        config.high_interest_bonus,
    )
    blocked = negative_hit(scored_text, config.negative_keyword_list())
    opportunity, question, gap = _opportunity_factor(state, trigger_text, now)
    factors = SocialFactors(
        addressed_to_me=ADDRESSED_FACTOR if addressed else 1.0,
        conversation_relevance=interest,
        conversation_activity=activity_factor(rate_in_window(state, now, 60)),
        social_opportunity=opportunity,
        recent_reply_frequency=_frequency_factor(state, now),
        social_energy=max(state.social_energy, ENERGY_FACTOR_FLOOR),
        matched_keywords=matched,
        negative_hit=blocked,
        question_like=question,
        quiet_gap_seconds=max(0.0, gap),
    )
    if blocked is not None:
        factors.total = 0.0
        return factors

    factors.total = max(
        0.0,
        min(
            factors.addressed_to_me
            * factors.conversation_relevance
            * factors.conversation_activity
            * factors.social_opportunity
            * factors.recent_reply_frequency
            * factors.social_energy,
            SCORE_CAP,
        ),
    )
    return factors


def format_factors(factors: SocialFactors) -> str:
    """Compact JSON of the factor values, for the decision prompt / logs."""
    return json.dumps(
        {
            "addressed_to_me": round(factors.addressed_to_me, 2),
            "conversation_relevance": round(factors.conversation_relevance, 2),
            "conversation_activity": round(factors.conversation_activity, 2),
            "social_opportunity": round(factors.social_opportunity, 2),
            "recent_reply_frequency": round(factors.recent_reply_frequency, 2),
            "social_energy": round(factors.social_energy, 2),
        },
        ensure_ascii=False,
    )
