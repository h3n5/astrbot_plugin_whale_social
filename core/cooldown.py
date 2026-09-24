"""Cooldown tiers and the reply window."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.config import PluginConfig
    from core.models import GroupState

# Tier multipliers are applied to the configured (min, max) cooldown range.
TIER_MULTIPLIERS: dict[str, tuple[float, float]] = {
    "normal": (1.0, 1.0),
    "recent": (1.0 / 3.0, 0.5),
    "consecutive": (2.0, 2.0),
    "ignored": (2.0, 3.0),
}


def select_tier(state: "GroupState") -> str:
    if state.last_bot_ignored:
        return "ignored"
    if state.consecutive_bot_messages >= 1:
        return "consecutive"
    if state.last_bot_message_time > 0:
        return "recent"
    return "normal"


def next_cooldown_seconds(state: "GroupState", config: "PluginConfig", rng) -> float:
    low_mult, high_mult = TIER_MULTIPLIERS[select_tier(state)]
    low = max(0.0, config.min_cooldown_seconds * low_mult)
    high = max(low, config.max_cooldown_seconds * high_mult)
    return rng.uniform(low, high)


def in_cooldown(state: "GroupState", now: float) -> bool:
    return state.next_speak_after > 0 and now < state.next_speak_after


def schedule_next_speak(
    state: "GroupState",
    config: "PluginConfig",
    now: float,
    rng,
) -> float:
    """Draw a fresh cooldown once and persist the deadline on ``state``.

    Called only after a successful proactive send, so the threshold does not
    jitter between checks (fixes the v1 bug).
    """
    state.next_speak_after = now + next_cooldown_seconds(state, config, rng)
    return state.next_speak_after


def open_reply_window(state: "GroupState", config: "PluginConfig", now: float) -> None:
    state.awaiting_reply_until = now + config.reply_window_seconds
    state.last_bot_ignored = False


def settle_reply_window(state: "GroupState", now: float) -> bool:
    """Mark ``last_bot_ignored`` once the window expires with no human reply.

    Lazy settlement: called whenever a message is observed or a decision runs,
    so no scheduler is required.
    """
    if state.awaiting_reply_until <= 0:
        return False
    if now <= state.awaiting_reply_until:
        return False
    state.awaiting_reply_until = 0.0
    state.last_bot_ignored = True
    return True


def note_human_reply(state: "GroupState", now: float) -> bool:
    """Clear the reply window when a human answers inside it."""
    if state.awaiting_reply_until <= 0 or now > state.awaiting_reply_until:
        return False
    state.awaiting_reply_until = 0.0
    state.last_bot_ignored = False
    return True
