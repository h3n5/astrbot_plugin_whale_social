from core.config import PluginConfig
from core.models import GroupState
from core.scorer import activity_factor, compute_score

NOW = 1_700_000_000.0


def _config(**overrides) -> PluginConfig:
    base = dict(interest_keywords="副本\n抽卡", negative_keywords="吵架")
    base.update(overrides)
    return PluginConfig(**base)


def test_activity_factor_thresholds():
    assert activity_factor(0) == 1.2
    assert activity_factor(2) == 1.2
    assert activity_factor(3) == 1.0
    assert activity_factor(10) == 1.0
    assert activity_factor(11) == 0.7
    assert activity_factor(20) == 0.7
    assert activity_factor(21) == 0.35


def test_keyword_hit_raises_interest():
    breakdown = compute_score(GroupState(), "今晚打副本吗", _config(), NOW)
    assert breakdown.topic_interest > 1.0
    assert "副本" in breakdown.matched_keywords
    assert breakdown.total > 0


def test_negative_keyword_zeroes_score():
    breakdown = compute_score(GroupState(), "你们别吵架了", _config(), NOW)
    assert breakdown.negative_hit == "吵架"
    assert breakdown.total == 0.0


def test_ignored_state_lowers_score():
    baseline = compute_score(GroupState(), "一起打副本", _config(), NOW).total
    ignored = compute_score(
        GroupState(last_bot_ignored=True), "一起打副本", _config(), NOW
    ).total
    assert ignored < baseline


def test_consecutive_state_lowers_score():
    baseline = compute_score(GroupState(), "一起打副本", _config(), NOW).total
    chained = compute_score(
        GroupState(consecutive_bot_messages=1), "一起打副本", _config(), NOW
    ).total
    assert chained < baseline


def test_active_chat_lowers_score():
    quiet = compute_score(GroupState(), "一起打副本", _config(), NOW).total
    busy_state = GroupState(message_times=[NOW - 1] * 25)
    busy = compute_score(busy_state, "一起打副本", _config(), NOW).total
    assert busy < quiet


def test_score_is_clamped():
    breakdown = compute_score(GroupState(), "副本 抽卡 副本 抽卡", _config(), NOW)
    assert 0.0 <= breakdown.total <= 3.0


def test_per_group_keyword_override():
    config = _config(group_keyword_overrides={"umo": ["钓鱼"]})
    breakdown = compute_score(GroupState(), "今天去钓鱼", config, NOW, umo="umo")
    assert "钓鱼" in breakdown.matched_keywords
