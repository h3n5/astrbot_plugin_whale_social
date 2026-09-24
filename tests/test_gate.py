from datetime import datetime

from core.config import PluginConfig
from core.gate import check_gate, rate_in_window, within_active_hours
from core.models import GroupState

NOW = 1_700_000_000.0


def _moment(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute)


def _config(**overrides) -> PluginConfig:
    base = dict(group_allowlist=["umo"], active_hours="")
    base.update(overrides)
    return PluginConfig(**base)


def test_allows_quiet_group_in_active_hours():
    result = check_gate(GroupState(), _config(), NOW, _moment(12))
    assert result.allowed is True
    assert result.reason == "ok"


def test_disabled_plugin_blocks():
    result = check_gate(GroupState(), _config(enabled=False), NOW, _moment(12))
    assert result.reason == "disabled"


def test_mention_is_allowed_even_in_cooldown():
    state = GroupState(next_speak_after=NOW + 100)
    result = check_gate(state, _config(), NOW, _moment(12), mentioned=True)
    assert result.allowed is True
    assert result.reason == "mentioned"


def test_cooldown_blocks():
    state = GroupState(next_speak_after=NOW + 10)
    assert check_gate(state, _config(), NOW, _moment(12)).reason == "cooldown"


def test_rate_limit_blocks_flood():
    state = GroupState(message_times=[NOW - 1] * 31)
    result = check_gate(state, _config(incoming_rate_limit=30), NOW, _moment(12))
    assert result.reason == "rate_limit"
    assert rate_in_window(state, NOW, 30) == 31


def test_consecutive_bot_messages_block():
    state = GroupState(consecutive_bot_messages=1)
    assert check_gate(state, _config(), NOW, _moment(12)).reason == "consecutive_bot"


def test_quiet_hours_block():
    result = check_gate(GroupState(), _config(active_hours="08:00-12:00"), NOW, _moment(13))
    assert result.reason == "quiet_hours"


def test_daily_cap_blocks():
    state = GroupState(proactive_sent_today=5)
    result = check_gate(state, _config(daily_proactive_cap=5), NOW, _moment(12))
    assert result.reason == "daily_cap"


def test_active_hours_boundaries():
    assert within_active_hours("08:00-12:00", _moment(8)) is True
    assert within_active_hours("08:00-12:00", _moment(11, 59)) is True
    assert within_active_hours("08:00-12:00", _moment(12)) is False
    assert within_active_hours("08:00-12:00", _moment(7, 59)) is False


def test_active_hours_cross_midnight():
    assert within_active_hours("22:00-06:00", _moment(23)) is True
    assert within_active_hours("22:00-06:00", _moment(5)) is True
    assert within_active_hours("22:00-06:00", _moment(12)) is False


def test_invalid_active_hours_means_always_on():
    assert within_active_hours("not-a-time", _moment(3)) is True
    assert within_active_hours("", _moment(3)) is True
