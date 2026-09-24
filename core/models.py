"""Data models for the social engine.

These types are deliberately independent of AstrBot so the policy layer can be
unit-tested without a live bot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 1


@dataclass
class ChatMessage:
    """A single observed message."""

    message_id: str
    sender: str
    text: str
    timestamp: float
    is_bot: bool = False
    kind: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "text": self.text,
            "timestamp": self.timestamp,
            "is_bot": self.is_bot,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage":
        return cls(
            message_id=str(data.get("message_id", "")),
            sender=str(data.get("sender", "")),
            text=str(data.get("text", "")),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
            is_bot=bool(data.get("is_bot", False)),
            kind=str(data.get("kind", "text")),
        )


# Fields that survive a restart. The message window and rate buckets are
# intentionally NOT persisted: their timestamps go stale and would pollute the
# next session's context.
_PERSISTED_FIELDS: tuple[str, ...] = (
    "social_energy",
    "next_speak_after",
    "last_bot_message_time",
    "last_user_message_time",
    "last_bot_ignored",
    "consecutive_bot_messages",
    "last_proactive_msg",
    "awaiting_reply_until",
    "proactive_sent_today",
    "daily_reset_date",
    "shown_topic",
)


@dataclass
class GroupState:
    """Per-UMO social state."""

    # Persisted: pacing / counters.
    social_energy: float = 0.6
    next_speak_after: float = 0.0
    last_bot_message_time: float = 0.0
    last_user_message_time: float = 0.0
    last_bot_ignored: bool = False
    consecutive_bot_messages: int = 0
    last_proactive_msg: str = ""
    awaiting_reply_until: float = 0.0
    proactive_sent_today: int = 0
    daily_reset_date: str = ""
    shown_topic: str = ""

    # Runtime-only.
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_times: list[float] = field(default_factory=list)
    seen_message_ids: list[str] = field(default_factory=list)
    mentioned_until: float = 0.0

    def to_persist_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _PERSISTED_FIELDS}

    @classmethod
    def from_persist_dict(
        cls,
        data: Mapping[str, Any],
        *,
        energy_initial: float = 0.6,
    ) -> "GroupState":
        state = cls(social_energy=float(energy_initial))
        for name in _PERSISTED_FIELDS:
            if name not in data:
                continue
            value = data[name]
            default = getattr(state, name)
            try:
                if isinstance(default, bool):
                    setattr(state, name, bool(value))
                elif isinstance(default, int) and not isinstance(default, bool):
                    setattr(state, name, int(value))
                elif isinstance(default, float):
                    setattr(state, name, float(value))
                else:
                    setattr(state, name, "" if value is None else str(value))
            except (TypeError, ValueError):
                # Ignore malformed field; keep the default.
                continue
        return state

    @property
    def topic(self) -> str:
        return self.shown_topic
