"""Memory hints and write-back payloads (no AstrBot imports).

The actual AstrBot conversation-manager calls live in ``main.py``; this module
only builds the textual payloads so they can be unit-tested.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .gate import rate_in_window

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import PluginConfig
    from .models import GroupState


def _minutes(value: float) -> str:
    if value <= 0:
        return "未知"
    minutes = int(value // 60)
    if minutes < 1:
        return "不到 1 分钟"
    return f"{minutes} 分钟"


def build_group_hint(
    state: "GroupState",
    config: "PluginConfig",
    now: float,
) -> Optional[str]:
    """Short dynamic context for LLM prompts.

    Used in two places: the proactive decision prompt (always, so the decision
    model sees the room's pulse) and the reply-injection into normal LLM
    requests (gated by ``inject_group_context`` at the adapter layer in
    ``main.py``). The block is meant to be marked temporary so it never
    pollutes the persisted system prompt.
    """
    last_user_gap = now - state.last_user_message_time if state.last_user_message_time else 0.0
    lines = [
        "<dynamic_context>",
        "以下是与当前群聊氛围有关的临时状态，只用于调整说话时机，不要直接复述：",
        f"- 最近 60 秒人类消息数：{rate_in_window(state, now, 60)}",
        f"- 距离上一条用户消息：{_minutes(last_user_gap)}",
        f"- 社交能量：{state.social_energy:.2f}",
        f"- 上一条主动发言是否无人回应：{'是' if state.last_bot_ignored else '否'}",
        f"- 最近连续机器人发言数：{state.consecutive_bot_messages}",
    ]
    if state.shown_topic:
        lines.append(f"- 当前关注话题：{state.shown_topic}")
    lines.append("</dynamic_context>")
    return "\n".join(lines)


def build_writeback_user_text(trigger_text: Optional[str]) -> str:
    """User-side text used when writing a proactive reply into memory."""
    value = (trigger_text or "").strip()
    return value or "（群聊中主动搭话）"
