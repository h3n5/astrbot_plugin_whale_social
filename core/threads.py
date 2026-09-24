"""Conversation threads: clustering, activity and selection.

The goal is to answer "which conversation should the bot join?" instead of
"which user should it reply to?". Everything here is pure Python and works on
message dicts produced by :class:`core.models.ChatMessage`.

No AstrBot imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Optional

from core.topic import keyword_hits

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.config import PluginConfig
    from core.models import GroupState

TARGET_GROUP = "GROUP"
TARGET_USER = "USER"


@dataclass
class ConversationTarget:
    type: str = TARGET_GROUP
    user_id: Optional[str] = None


@dataclass
class ConversationThread:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    topic: str = ""
    created_at: float = 0.0
    last_activity: float = 0.0
    activity_score: float = 0.0
    bot_participated: bool = False
    last_bot_message_time: float = 0.0
    target: ConversationTarget = field(default_factory=ConversationTarget)
    ended: bool = False


# -- internals ------------------------------------------------------------


def _add_participant(thread: ConversationThread, sender: str) -> None:
    if sender and sender not in thread.participants:
        thread.participants.append(sender)


def _joined_text(thread: ConversationThread) -> str:
    return " ".join(str(message.get("text", "")) for message in thread.messages)


def _update_topic(thread: ConversationThread, text: str, keywords: Iterable[str]) -> None:
    if thread.topic:
        return
    hits = keyword_hits(text, keywords)
    if hits:
        thread.topic = hits[0]


def compute_activity(thread: ConversationThread, config: "PluginConfig", now: float) -> float:
    window = float(config.thread_window_seconds)
    if window > 0:
        recent = [
            message
            for message in thread.messages
            if now - float(message.get("timestamp", now)) <= window
        ]
    else:
        recent = list(thread.messages)
    half_life = window if window > 0 else 60.0
    decay = math.exp(-max(0.0, now - thread.last_activity) / half_life) if half_life > 0 else 1.0
    participants = max(1, len(thread.participants))
    score = len(recent) * (1.0 + 0.25 * (participants - 1)) * decay
    if thread.bot_participated:
        score *= 1.3
    return score


def add_message_to_thread(
    thread: ConversationThread,
    message: dict[str, Any],
    config: "PluginConfig",
    now: float,
    keywords: Iterable[str],
) -> None:
    thread.messages.append(message)
    _add_participant(thread, str(message.get("sender", "")))
    thread.last_activity = now
    if message.get("is_bot"):
        thread.bot_participated = True
        thread.last_bot_message_time = now
    _update_topic(thread, str(message.get("text", "")), keywords)
    thread.activity_score = compute_activity(thread, config, now)


def _thread_keyword_match(thread: ConversationThread, text: str, keywords: Iterable[str]) -> bool:
    message_hits = set(keyword_hits(text, keywords))
    if not message_hits:
        return False
    thread_hits = set(keyword_hits(_joined_text(thread), keywords))
    return bool(message_hits & thread_hits)


def _new_thread(
    state: "GroupState",
    message: dict[str, Any],
    config: "PluginConfig",
    now: float,
    keywords: Iterable[str],
) -> ConversationThread:
    state.thread_seq = int(getattr(state, "thread_seq", 0)) + 1
    thread = ConversationThread(id=f"t{state.thread_seq}", created_at=now)
    add_message_to_thread(thread, message, config, now, keywords)
    state.threads.append(thread)
    return thread


def _active_threads(state: "GroupState", now: float, window: float) -> list[ConversationThread]:
    result = []
    for thread in state.threads:
        if thread.ended:
            continue
        if window > 0 and now - thread.last_activity > window:
            continue
        result.append(thread)
    return result


# -- public API -----------------------------------------------------------


def assign_thread(
    state: "GroupState",
    message: dict[str, Any],
    config: "PluginConfig",
    now: float,
    *,
    umo: Optional[str] = None,
) -> ConversationThread:
    """Attach a message to the best matching thread, or start a new one.

    Matching priority: reply relation -> @ relation -> keyword overlap ->
    participant overlap -> new thread.
    """
    keywords = config.interest_keyword_list(umo)
    gap = float(config.thread_join_time_gap_seconds)

    # 1. Explicit quote/reply to a known message.
    reply_to = str(message.get("reply_to", "") or "")
    if reply_to:
        for thread in state.threads:
            if any(str(item.get("message_id", "")) == reply_to for item in thread.messages):
                return thread

    # 2. @ someone who recently spoke in a thread -> that thread.
    at_users = [str(user) for user in (message.get("at_users") or [])]
    if at_users:
        best: Optional[ConversationThread] = None
        for thread in state.threads:
            if thread.ended:
                continue
            for item in reversed(thread.messages):
                if item.get("is_bot"):
                    continue
                if str(item.get("sender", "")) in at_users:
                    if best is None or thread.last_activity > best.last_activity:
                        best = thread
                    break
        if best is not None:
            return best

    # 3. Time-bounded keyword / participant overlap.
    candidates = [
        thread
        for thread in state.threads
        if not thread.ended and (gap <= 0 or now - thread.last_activity <= gap)
    ]
    if candidates:
        text = str(message.get("text", ""))
        keyword_candidates = [
            thread for thread in candidates if _thread_keyword_match(thread, text, keywords)
        ]
        pool = keyword_candidates or [
            thread
            for thread in candidates
            if str(message.get("sender", "")) in thread.participants
        ]
        if pool:
            return max(pool, key=lambda thread: thread.last_activity)

    # 4. New conversation.
    return _new_thread(state, message, config, now, keywords)


def refresh_activities(state: "GroupState", config: "PluginConfig", now: float) -> None:
    for thread in state.threads:
        thread.activity_score = compute_activity(thread, config, now)


def prune_threads(state: "GroupState", config: "PluginConfig", now: float) -> None:
    """Mark stale threads ended and cap how many threads we keep."""
    window = float(config.thread_window_seconds)
    for thread in state.threads:
        if window > 0 and now - thread.last_activity > window:
            thread.ended = True

    cap = int(config.max_threads)
    if cap > 0:
        active = [thread for thread in state.threads if not thread.ended]
        if len(active) > cap:
            active.sort(key=lambda thread: thread.activity_score, reverse=True)
            keep = {id(thread) for thread in active[:cap]}
            for thread in state.threads:
                if not thread.ended and id(thread) not in keep:
                    thread.ended = True

    # Bound the retained list (ended threads are still useful for reply joins).
    limit = max(cap, 1) * 4 if cap > 0 else 20
    if len(state.threads) > limit:
        state.threads.sort(key=lambda thread: thread.last_activity, reverse=True)
        del state.threads[limit:]


def find_thread(state: "GroupState", thread_id: str) -> Optional[ConversationThread]:
    if not thread_id:
        return None
    for thread in state.threads:
        if thread.id == thread_id:
            return thread
    return None


def thread_is_active(thread: ConversationThread, config: "PluginConfig", now: float) -> bool:
    if thread.ended:
        return False
    window = float(config.thread_window_seconds)
    return window <= 0 or now - thread.last_activity <= window


def last_human_message(thread: ConversationThread) -> Optional[dict[str, Any]]:
    for message in reversed(thread.messages):
        if not message.get("is_bot"):
            return message
    return None


def _interest_count(thread: ConversationThread, keywords: Iterable[str]) -> int:
    return len(keyword_hits(_joined_text(thread), keywords))


def select_thread(
    state: "GroupState",
    config: "PluginConfig",
    now: float,
    *,
    umo: Optional[str] = None,
) -> Optional[ConversationThread]:
    """Pick the single thread most worth joining, or ``None``."""
    window = float(config.thread_window_seconds)
    minimum = max(1, int(config.min_thread_messages))
    candidates = [
        thread
        for thread in _active_threads(state, now, window)
        if len(thread.messages) >= minimum
    ]
    if not candidates:
        return None

    keywords = config.interest_keyword_list(umo)
    if config.thread_selection == "interest":
        return max(
            candidates,
            key=lambda thread: (
                _interest_count(thread, keywords),
                thread.activity_score,
                thread.last_activity,
            ),
        )
    return max(
        candidates,
        key=lambda thread: (thread.activity_score, thread.last_activity),
    )


def format_overview(
    state: "GroupState",
    config: "PluginConfig",
    now: float,
    *,
    exclude_id: str = "",
) -> str:
    """Human/LLM readable list of the other active threads."""
    window = float(config.thread_window_seconds)
    threads = sorted(
        _active_threads(state, now, window),
        key=lambda thread: thread.activity_score,
        reverse=True,
    )
    lines = []
    for thread in threads:
        if thread.id == exclude_id:
            continue
        lines.append(
            f"- {thread.id}｜话题：{thread.topic or '未识别'}｜"
            f"参与者：{len(thread.participants)}｜消息：{len(thread.messages)}｜"
            f"活跃度：{thread.activity_score:.2f}"
        )
    return "\n".join(lines)
