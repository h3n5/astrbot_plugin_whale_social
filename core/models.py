"""Data models for the social engine.

These types are deliberately independent of AstrBot so the policy layer can be
unit-tested without a live bot.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 2


@dataclass
class ChatMessage:
    """A single observed message."""

    message_id: str
    sender: str
    text: str
    timestamp: float
    is_bot: bool = False
    kind: str = "text"
    reply_to: str = ""
    at_users: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "text": self.text,
            "timestamp": self.timestamp,
            "is_bot": self.is_bot,
            "kind": self.kind,
            "reply_to": self.reply_to,
            "at_users": list(self.at_users),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChatMessage":
        raw_at = data.get("at_users") or []
        if isinstance(raw_at, (list, tuple, set)):
            at_users = [str(item) for item in raw_at]
        else:
            at_users = []
        return cls(
            message_id=str(data.get("message_id", "")),
            sender=str(data.get("sender", "")),
            text=str(data.get("text", "")),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
            is_bot=bool(data.get("is_bot", False)),
            kind=str(data.get("kind", "text")),
            reply_to=str(data.get("reply_to", "") or ""),
            at_users=at_users,
        )


@dataclass
class MessageEnvelope:
    """Adapter-neutral view of one observed event.

    Built once at the edge of the engine so every downstream layer (dedup,
    collector, threads) works from the same normalized fields instead of the
    raw AstrBot event.
    """

    umo: str
    message_id: str
    sender: str
    text: str
    timestamp: float
    is_bot: bool = False
    kind: str = "text"
    mentioned: bool = False
    reply_to: str = ""
    at_users: list[str] = field(default_factory=list)

    def dedup_key(self) -> str:
        """Stable exact key for reconnect-replay de-duplication.

        Empty when the platform gave no ``message_id``; callers must then fall
        back (or not de-duplicate at all) rather than let every id-less event
        collide under one key.
        """
        mid = (self.message_id or "").strip()
        if not mid:
            return ""
        return f"{self.umo}:{mid}"

    def fingerprint(self, bucket_seconds: float) -> str:
        """Coarse fallback key for id-less events (disabled when <= 0).

        Only meant as an anomaly aid: without a real id, two genuine identical
        messages in the same bucket are indistinguishable from one replay.
        """
        if bucket_seconds <= 0:
            return ""
        bucket = int(self.timestamp // bucket_seconds)
        digest = hashlib.sha1((self.text or "").encode("utf-8")).hexdigest()[:12]
        return f"fp:{self.umo}:{self.sender}:{digest}:{bucket}"


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
    "group_tokens",
    "group_tokens_updated_at",
    "failure_count",
    "send_blocked_until",
    "llm_failure_count",
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

    # Flow control (persisted): token bucket + failure backoff.
    group_tokens: float = 0.0
    group_tokens_updated_at: float = 0.0
    failure_count: int = 0
    send_blocked_until: float = 0.0
    # Consecutive decision-model failures (best-effort metric, persisted).
    llm_failure_count: int = 0

    # Runtime-only.
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_times: list[float] = field(default_factory=list)
    seen_message_ids: list[str] = field(default_factory=list)
    mentioned_until: float = 0.0
    threads: list[Any] = field(default_factory=list)  # list[ConversationThread]
    thread_seq: int = 0
    revision: int = 0
    debounce_deadline: float = 0.0
    debounce_first_trigger: float = 0.0
    selected_thread_id: str = ""
    # One-shot reservation for the in-flight decision (observability + guard).
    reserved_thread_id: str = ""
    reserved_revision: int = 0
    # Marker for our own just-sent message so a platform echo is not counted twice.
    local_outgoing_at: float = 0.0
    local_outgoing_text: str = ""

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
