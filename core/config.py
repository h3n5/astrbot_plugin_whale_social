"""Typed, AstrBot-independent view of the plugin configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

DEFAULT_INTEREST_KEYWORDS = (
    "游戏\n副本\nBoss\n活动\n抽卡\n角色\n装备\n攻略\n剧情\n动漫\n老婆\n整活\n哈哈\n笑死"
)

DEFAULT_PERSONA_PROMPT = (
    "你是鲸鱼娘，一个游戏群里的虚拟群友。\n\n"
    "你喜欢游戏、动漫和群聊里的各种趣事。\n"
    "你平时比较喜欢潜水，不会为了说话而说话。\n"
    "看到自己感兴趣的话题时，会像普通群友一样自然参与。\n\n"
    "你的回复应该简短、自然、口语化。\n"
    "不要主动强调自己是 AI、机器人或程序。\n"
    "不要解释自己的行为逻辑。\n"
    "不要为了延续聊天而强行提问。\n"
    "不要连续刷屏。"
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
9. 回复要像普通群友，而不是客服。
10. 不要解释“为什么要回复”。

只允许输出一个 JSON 对象，不要输出其他内容：
{
  "action": "IGNORE | WAIT | SPEAK",
  "reason": "简短原因",
  "topic": "当前话题",
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
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for umo, keywords in value.items():
        parsed = split_lines(keywords)
        if parsed:
            result[str(umo)] = parsed
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
    active_hours: str = "08:00-23:59"
    provider_id: str = ""
    interest_keywords: str = DEFAULT_INTEREST_KEYWORDS
    negative_keywords: str = ""
    group_keyword_overrides: dict[str, list[str]] = field(default_factory=dict)
    output_blocklist: str = ""
    use_astrbot_memory: bool = True
    memory_writeback: bool = True
    inject_group_context: bool = True
    persona_prompt: str = DEFAULT_PERSONA_PROMPT
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
            active_hours=_as_str(_get(mapping, "active_hours", "08:00-23:59"), "08:00-23:59"),
            provider_id=_as_str(_get(mapping, "provider_id", ""), ""),
            interest_keywords=_as_str(_get(mapping, "interest_keywords", DEFAULT_INTEREST_KEYWORDS), DEFAULT_INTEREST_KEYWORDS),
            negative_keywords=_as_str(_get(mapping, "negative_keywords", ""), ""),
            group_keyword_overrides=_as_keyword_overrides(_get(mapping, "group_keyword_overrides", {})),
            output_blocklist=_as_str(_get(mapping, "output_blocklist", ""), ""),
            use_astrbot_memory=_as_bool(_get(mapping, "use_astrbot_memory", True), True),
            memory_writeback=_as_bool(_get(mapping, "memory_writeback", True), True),
            inject_group_context=_as_bool(_get(mapping, "inject_group_context", True), True),
            persona_prompt=_as_str(_get(mapping, "persona_prompt", DEFAULT_PERSONA_PROMPT), DEFAULT_PERSONA_PROMPT),
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
