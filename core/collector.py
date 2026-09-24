"""Message collection: window, dedupe, rate buckets, energy, outgoing bookkeeping."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from core.cooldown import note_human_reply, open_reply_window, schedule_next_speak
from core.models import ChatMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.config import PluginConfig
    from core.models import GroupState

ENERGY_DRAIN = 0.08
RATE_WINDOW_SECONDS = 300.0
SEEN_ID_LIMIT = 200


class MessageCollector:
    def __init__(self, config: "PluginConfig") -> None:
        self.config = config

    def record(
        self,
        state: "GroupState",
        *,
        message_id: str,
        sender: str,
        text: str,
        is_bot: bool,
        kind: str,
        now: float,
    ) -> Optional[ChatMessage]:
        """Record an observed message.

        Returns ``None`` when the event is a duplicate (same ``message_id``).
        Human messages reset the bot streak and may close the reply window;
        bot messages increase the streak.
        """
        mid = str(message_id or "")
        if mid:
            if mid in state.seen_message_ids:
                return None
            state.seen_message_ids.append(mid)
            overflow = len(state.seen_message_ids) - SEEN_ID_LIMIT
            if overflow > 0:
                del state.seen_message_ids[:overflow]

        message = ChatMessage(
            message_id=mid,
            sender=str(sender),
            text=str(text or ""),
            timestamp=now,
            is_bot=bool(is_bot),
            kind=kind or "text",
        )
        state.messages.append(message.to_dict())
        limit = max(1, int(self.config.context_message_limit))
        overflow = len(state.messages) - limit
        if overflow > 0:
            del state.messages[:overflow]

        if is_bot:
            state.last_bot_message_time = now
            state.consecutive_bot_messages += 1
            return message

        # Human message.
        state.last_user_message_time = now
        state.consecutive_bot_messages = 0  # P0-1: never stay muted forever
        note_human_reply(state, now)
        state.message_times.append(now)
        cutoff = now - RATE_WINDOW_SECONDS
        state.message_times = [t for t in state.message_times if t >= cutoff]
        return message

    def note_outgoing(
        self,
        state: "GroupState",
        config: "PluginConfig",
        text: str,
        *,
        now: float,
        rng,
    ) -> None:
        """Bookkeeping after a successful proactive send."""
        state.last_proactive_msg = text
        # Draw the cooldown before mutating streak / ignored / last-bot-time so
        # the tier reflects the state *before* this send (a first message uses
        # the normal range rather than always looking "recent").
        schedule_next_speak(state, config, now, rng)
        state.last_bot_message_time = now
        state.consecutive_bot_messages += 1
        open_reply_window(state, config, now)
        state.social_energy = max(
            0.1, min(state.social_energy - ENERGY_DRAIN, config.energy_max)
        )
        state.proactive_sent_today += 1

    def build_context(self, state: "GroupState") -> str:
        limit = max(1, int(self.config.context_message_limit))
        lines: list[str] = []
        for item in state.messages[-limit:]:
            try:
                stamp = float(item.get("timestamp", 0.0))
                ts = datetime.fromtimestamp(stamp).strftime("%H:%M:%S")
            except (TypeError, ValueError, OSError):
                ts = "--:--:--"
            mark = " [鲸鱼娘]" if item.get("is_bot") else ""
            text = str(item.get("text", ""))
            kind = str(item.get("kind", "text"))
            if not text and kind != "text":
                text = f"[{kind}]"
            lines.append(f"[{ts}] {item.get('sender', '?')}{mark}: {text}")
        return "\n".join(lines)
