"""Tests for group-hint construction and writeback payloads."""

from core.config import PluginConfig
from core.memory import build_group_hint, build_writeback_user_text
from core.models import GroupState


def test_group_hint_is_independent_of_inject_flag():
    """The decision prompt always gets the hint; the reply-injection gate
    lives in main.py, so ``inject_group_context`` must not strip it here."""
    config = PluginConfig(inject_group_context=False)
    hint = build_group_hint(GroupState(social_energy=0.5), config, now=1000.0)
    assert hint is not None
    assert "社交能量：0.50" in hint


def test_group_hint_contains_room_pulse():
    config = PluginConfig()
    state = GroupState(social_energy=0.4, consecutive_bot_messages=2, shown_topic="副本")
    hint = build_group_hint(state, config, now=1000.0)
    assert hint is not None
    assert "最近连续机器人发言数：2" in hint
    assert "当前关注话题：副本" in hint


def test_writeback_user_text_falls_back_for_empty_trigger():
    assert build_writeback_user_text("打副本吗") == "打副本吗"
    assert build_writeback_user_text("   ") == "（群聊中主动搭话）"
    assert build_writeback_user_text(None) == "（群聊中主动搭话）"
