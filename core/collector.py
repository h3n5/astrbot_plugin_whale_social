"""Message collection: window, dedupe, rate buckets, energy, outgoing bookkeeping."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .cooldown import note_human_reply, open_reply_window, schedule_next_speak
from .models import ChatMessage
from .timeutil import local_datetime

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import PluginConfig
    from .models import GroupState

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
        reply_to: str = "",
        at_users: Optional[list[str]] = None,
        close_reply_window: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Record an observed message.

        Returns the stored message dict, or ``None`` when the event is a
        duplicate (same ``message_id``). Human messages reset the bot streak
        and may close the reply window; bot messages increase the streak.

        ``close_reply_window`` lets the caller decide whether this message
        actually answers the bot: unrelated chatter must not clear the
        "ignored" flag, so the engine passes ``False`` and settles the window
        itself once it knows the thread relation.
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
            reply_to=str(reply_to or ""),
            at_users=[str(user) for user in (at_users or [])],
        )
        payload = message.to_dict()
        state.messages.append(payload)
        limit = max(1, int(self.config.context_message_limit))
        overflow = len(state.messages) - limit
        if overflow > 0:
            del state.messages[:overflow]

        if is_bot:
            state.last_bot_message_time = now
            state.consecutive_bot_messages += 1
            return payload

        # Human message.
        state.last_user_message_time = now
        state.consecutive_bot_messages = 0  # P0-1: never stay muted forever
        if close_reply_window:
            note_human_reply(state, now)
        state.message_times.append(now)
        cutoff = now - RATE_WINDOW_SECONDS
        state.message_times = [t for t in state.message_times if t >= cutoff]
        return payload

    def note_outgoing(
        self,
        state: "GroupState",
        config: "PluginConfig",
        text: str,
        *,
        now: float,
        rng,
    ) -> dict[str, Any]:
        """Bookkeeping after a successful proactive send.

        Also records the message locally so threads/context do not depend on
        the platform echoing our own message back; the echo (if any) is
        suppressed via the ``local_outgoing_*`` marker.
        """
        state.last_proactive_msg = text
        # Track send times for the recent_reply_frequency social factor; the
        # one-hour retention bounds the list while covering the 30-min window.
        state.proactive_send_times.append(now)
        send_cutoff = now - 3600.0
        state.proactive_send_times = [
            stamp for stamp in state.proactive_send_times if stamp >= send_cutoff
        ]
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

        message = ChatMessage(
            message_id=f"local-out-{int(now * 1000)}",
            sender="bot",
            text=str(text or ""),
            timestamp=now,
            is_bot=True,
            kind="text",
        )
        payload = message.to_dict()
        state.messages.append(payload)
        limit = max(1, int(config.context_message_limit))
        overflow = len(state.messages) - limit
        if overflow > 0:
            del state.messages[:overflow]
        state.local_outgoing_at = now
        state.local_outgoing_text = str(text or "")
        return payload

    def build_context(self, state: "GroupState") -> str:
        limit = max(1, int(self.config.context_message_limit))
        return self.format_messages(state.messages[-limit:])

    def format_messages(self, messages: list[dict[str, Any]]) -> str:
        """Render a list of stored messages as a readable transcript."""
        lines: list[str] = []
        for item in messages:
            try:
                stamp = float(item.get("timestamp", 0.0))
                # Same timezone as quotas / quiet hours, so the transcript
                # shown to the model (and in ws status) matches configuration.
                ts = local_datetime(stamp, self.config.timezone).strftime("%H:%M:%S")
            except (TypeError, ValueError, OSError, OverflowError):
                ts = "--:--:--"
            mark = " [鲸鱼娘]" if item.get("is_bot") else ""
            text = str(item.get("text", ""))
            kind = str(item.get("kind", "text"))
            if not text and kind != "text":
                text = f"[{kind}]"
            lines.append(f"[{ts}] {item.get('sender', '?')}{mark}: {text}")
        return "\n".join(lines)
