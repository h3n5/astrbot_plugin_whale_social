"""Typed, AstrBot-independent view of the plugin configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

DEFAULT_INTEREST_KEYWORDS = (
    "游戏\n副本\nBoss\n活动\n抽卡\n角色\n装备\n攻略\n剧情\n动漫\n老婆\n整活\n哈哈\n笑死"
)

DEFAULT_DECISION_PROMPT = """你是群聊机器人的“社交决策器”。

你的任务不是回答问题，而是判断机器人现在是否应该主动参与群聊。

行为原则：
1. 大多数时候应该潜水。
2. 不要为了说话而说话。
3. 话题与机器人兴趣高度相关时，可以参与。
4. 群聊正在高速连续刷屏时，不要强行插话。
5. 刚刚发过言且没人回应时，应明显降低再次发言意愿。
6. 普通无关闲聊选择 IGNORE。
7. 话题有意思但不是自然插话时机时选择 WAIT。
8. 只有真正适合插入时才 SPEAK。
9. 回复要自然、口语化，不要像客服。
10. 不要解释“为什么要回复”。
11. 优先选择最值得参与的一个会话（thread_id），不要同时参与多个话题。
12. target.type 默认 GROUP；只有确实是在回应某个具体群友时才用 USER，并在 user_id 填该群友 ID。

只允许输出一个 JSON 对象，不要输出其他内容：
{
  "action": "IGNORE | WAIT | SPEAK",
  "reason": "简短原因",
  "topic": "当前话题",
  "thread_id": "要参与的会话 id，应来自上文给出的 thread_id",
  "target": {"type": "GROUP | USER", "user_id": "target.type 为 USER 时填写群友 ID"},
  "reply": "action 为 SPEAK 时填写回复，否则为空字符串"
}"""


def _get(mapping: Any, key: str, default: Any) -> Any:
    if mapping is None:
        return default
    if isinstance(mapping, Mapping):
        value = mapping.get(key, default)
    else:  # AstrBotConfig and other dict-like objects
        getter = getattr(mapping, "get", None)
        value = getter(key, default) if callable(getter) else default
    return default if value is None else value


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def split_lines(value: Any) -> list[str]:
    """Split a text/list config value into non-empty stripped lines."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = str(value).splitlines()
    return [item.strip() for item in items if item.strip()]


def _as_allowlist(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return split_lines(value)


def _as_keyword_overrides(value: Any) -> dict[str, list[str]]:
    """Accept per-group keyword overrides in either supported shape.

    The WebUI schema exposes this option as a list of ``umo=kw1,kw2`` items
    (AstrBot requires an ``items`` sub-schema for object-type nodes, which a
    dynamic umo -> keywords map cannot provide). Hand-edited configs may
    still provide the raw mapping form.
    """
    if isinstance(value, Mapping):
        result: dict[str, list[str]] = {}
        for umo, keywords in value.items():
            parsed = split_lines(keywords)
            if parsed:
                result[str(umo)] = parsed
        return result

    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = split_lines(value)

    result = {}
    for item in items:
        entry = item.strip()
        if not entry or "=" not in entry:
            continue
        umo, _, raw = entry.partition("=")
        umo = umo.strip()
        keywords = [kw.strip() for kw in raw.replace("，", ",").split(",") if kw.strip()]
        if umo and keywords:
            result[umo] = keywords
    return result


def parse_hhmm(token: str) -> Optional[int]:
    """Parse ``HH:MM`` into minutes-since-midnight, or ``None``."""
    parts = token.strip().split(":")
    if not parts or not parts[0]:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def parse_active_hours(value: str) -> tuple[int, int]:
    """Return ``(start_minutes, end_minutes)``; ``(0, 1440)`` when unrestricted."""
    text = (value or "").strip()
    if not text or "-" not in text:
        return 0, 1440
    start_token, _, end_token = text.partition("-")
    start = parse_hhmm(start_token)
    end = parse_hhmm(end_token)
    if start is None or end is None:
        return 0, 1440
    return start, end


@dataclass
class PluginConfig:
    enabled: bool = True
    dry_run: bool = False
    group_allowlist: list[str] = field(default_factory=list)
    min_message_length: int = 2
    context_message_limit: int = 20
    incoming_rate_limit: int = 30
    min_cooldown_seconds: float = 900.0
    max_cooldown_seconds: float = 1800.0
    reply_window_seconds: float = 120.0
    base_speak_probability: float = 0.08
    high_interest_bonus: float = 1.8
    energy_initial: float = 0.6
    energy_max: float = 1.0
    daily_proactive_cap: int = 20
    global_daily_proactive_cap: int = 100
    group_token_bucket_capacity: int = 3
    group_token_refill_seconds: int = 900
    global_token_bucket_capacity: int = 10
    global_token_refill_seconds: int = 300
    send_failure_backoff_seconds: int = 300
    max_failure_backoff_seconds: int = 3600
    llm_failure_backoff_seconds: int = 60
    llm_timeout_seconds: int = 60
    timezone: str = "Asia/Shanghai"
    max_group_states: int = 100
    state_ttl_seconds: int = 604800
    active_hours: str = "08:00-23:59"
    # V2.0: conversation threads + debounce.
    debounce_seconds: float = 3.0
    debounce_max_wait_seconds: float = 8.0
    thread_window_seconds: float = 180.0
    thread_join_time_gap_seconds: float = 120.0
    max_threads: int = 3
    min_thread_messages: int = 1
    thread_selection: str = "most_active"
    thread_time_continuity_merge: bool = True
    reply_mention_user: bool = False
    extract_message_segments: bool = True
    # V2.0: reconnect-replay de-duplication.
    dedup_ttl_seconds: float = 300.0
    dedup_max_entries: int = 10000
    dedup_fallback_seconds: float = 0.0
    provider_id: str = ""
    interest_keywords: str = DEFAULT_INTEREST_KEYWORDS
    negative_keywords: str = ""
    group_keyword_overrides: dict[str, list[str]] = field(default_factory=dict)
    output_blocklist: str = ""
    max_reply_length: int = 200
    use_astrbot_memory: bool = True
    memory_writeback: bool = True
    inject_group_context: bool = True
    decision_prompt: str = ""

    @classmethod
    def from_mapping(cls, mapping: Any) -> "PluginConfig":
        return cls(
            enabled=_as_bool(_get(mapping, "enabled", True), True),
            dry_run=_as_bool(_get(mapping, "dry_run", False), False),
            group_allowlist=_as_allowlist(_get(mapping, "group_allowlist", [])),
            min_message_length=_as_int(_get(mapping, "min_message_length", 2), 2),
            context_message_limit=_as_int(_get(mapping, "context_message_limit", 20), 20),
            incoming_rate_limit=_as_int(_get(mapping, "incoming_rate_limit", 30), 30),
            min_cooldown_seconds=_as_float(_get(mapping, "min_cooldown_seconds", 900), 900.0),
            max_cooldown_seconds=_as_float(_get(mapping, "max_cooldown_seconds", 1800), 1800.0),
            reply_window_seconds=_as_float(_get(mapping, "reply_window_seconds", 120), 120.0),
            base_speak_probability=_as_float(_get(mapping, "base_speak_probability", 0.08), 0.08),
            high_interest_bonus=_as_float(_get(mapping, "high_interest_bonus", 1.8), 1.8),
            energy_initial=_as_float(_get(mapping, "energy_initial", 0.6), 0.6),
            energy_max=_as_float(_get(mapping, "energy_max", 1.0), 1.0),
            daily_proactive_cap=_as_int(_get(mapping, "daily_proactive_cap", 20), 20),
            global_daily_proactive_cap=_as_int(_get(mapping, "global_daily_proactive_cap", 100), 100),
            group_token_bucket_capacity=_as_int(_get(mapping, "group_token_bucket_capacity", 3), 3),
            group_token_refill_seconds=_as_int(_get(mapping, "group_token_refill_seconds", 900), 900),
            global_token_bucket_capacity=_as_int(_get(mapping, "global_token_bucket_capacity", 10), 10),
            global_token_refill_seconds=_as_int(_get(mapping, "global_token_refill_seconds", 300), 300),
            send_failure_backoff_seconds=_as_int(_get(mapping, "send_failure_backoff_seconds", 300), 300),
            max_failure_backoff_seconds=_as_int(_get(mapping, "max_failure_backoff_seconds", 3600), 3600),
            llm_failure_backoff_seconds=_as_int(_get(mapping, "llm_failure_backoff_seconds", 60), 60),
            llm_timeout_seconds=_as_int(_get(mapping, "llm_timeout_seconds", 60), 60),
            timezone=_as_str(_get(mapping, "timezone", "Asia/Shanghai"), "Asia/Shanghai"),
            max_group_states=_as_int(_get(mapping, "max_group_states", 100), 100),
            state_ttl_seconds=_as_int(_get(mapping, "state_ttl_seconds", 604800), 604800),
            active_hours=_as_str(_get(mapping, "active_hours", "08:00-23:59"), "08:00-23:59"),
            debounce_seconds=_as_float(_get(mapping, "debounce_seconds", 3), 3.0),
            debounce_max_wait_seconds=_as_float(_get(mapping, "debounce_max_wait_seconds", 8), 8.0),
            thread_window_seconds=_as_float(_get(mapping, "thread_window_seconds", 180), 180.0),
            thread_join_time_gap_seconds=_as_float(_get(mapping, "thread_join_time_gap_seconds", 120), 120.0),
            max_threads=_as_int(_get(mapping, "max_threads", 3), 3),
            min_thread_messages=_as_int(_get(mapping, "min_thread_messages", 1), 1),
            thread_selection=_as_str(_get(mapping, "thread_selection", "most_active"), "most_active"),
            thread_time_continuity_merge=_as_bool(_get(mapping, "thread_time_continuity_merge", True), True),
            reply_mention_user=_as_bool(_get(mapping, "reply_mention_user", False), False),
            extract_message_segments=_as_bool(_get(mapping, "extract_message_segments", True), True),
            dedup_ttl_seconds=_as_float(_get(mapping, "dedup_ttl_seconds", 300), 300.0),
            dedup_max_entries=_as_int(_get(mapping, "dedup_max_entries", 10000), 10000),
            dedup_fallback_seconds=_as_float(_get(mapping, "dedup_fallback_seconds", 0), 0.0),
            provider_id=_as_str(_get(mapping, "provider_id", ""), ""),
            interest_keywords=_as_str(_get(mapping, "interest_keywords", DEFAULT_INTEREST_KEYWORDS), DEFAULT_INTEREST_KEYWORDS),
            negative_keywords=_as_str(_get(mapping, "negative_keywords", ""), ""),
            group_keyword_overrides=_as_keyword_overrides(_get(mapping, "group_keyword_overrides", {})),
            output_blocklist=_as_str(_get(mapping, "output_blocklist", ""), ""),
            max_reply_length=_as_int(_get(mapping, "max_reply_length", 200), 200),
            use_astrbot_memory=_as_bool(_get(mapping, "use_astrbot_memory", True), True),
            memory_writeback=_as_bool(_get(mapping, "memory_writeback", True), True),
            inject_group_context=_as_bool(_get(mapping, "inject_group_context", True), True),
            decision_prompt=_as_str(_get(mapping, "decision_prompt", ""), ""),
        )

    # -- derived helpers -------------------------------------------------

    def interest_keyword_list(self, umo: Optional[str] = None) -> list[str]:
        if umo and umo in self.group_keyword_overrides:
            return list(self.group_keyword_overrides[umo])
        return split_lines(self.interest_keywords)

    def negative_keyword_list(self) -> list[str]:
        return split_lines(self.negative_keywords)

    def blocklist(self) -> list[str]:
        return split_lines(self.output_blocklist)

    def active_window(self) -> tuple[int, int]:
        return parse_active_hours(self.active_hours)
