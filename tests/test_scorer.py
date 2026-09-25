from core.config import PluginConfig
from core.models import GroupState
from core.scorer import activity_factor, compute_score, is_question_like

NOW = 1_700_000_000.0


def _config(**overrides) -> PluginConfig:
    base = dict(interest_keywords="副本\n抽卡", negative_keywords="吵架")
    base.update(overrides)
    return PluginConfig(**base)


def _state(**overrides) -> GroupState:
    state = GroupState(**overrides)
    state.last_user_message_time = NOW - 5.0
    return state


def test_activity_factor_thresholds():
    assert activity_factor(0) == 1.2
    assert activity_factor(2) == 1.2
    assert activity_factor(3) == 1.0
    assert activity_factor(10) == 1.0
    assert activity_factor(11) == 0.6
    assert activity_factor(20) == 0.6
    assert activity_factor(21) == 0.3


def test_keywordless_chatter_keeps_full_relevance():
    """Keywords are recall only: their absence must not mute the bot."""
    factors = compute_score(_state(), "今天天气还行", _config(), NOW, trigger_text="今天天气还行")
    assert factors.conversation_relevance == 1.0
    assert factors.total > 0


def test_keyword_hit_raises_relevance():
    factors = compute_score(_state(), "今晚打副本吗", _config(), NOW, trigger_text="今晚打副本吗")
    assert factors.conversation_relevance > 1.0
    assert "副本" in factors.matched_keywords
    assert factors.total > 0


def test_negative_keyword_zeroes_score():
    factors = compute_score(_state(), "你们别吵架了", _config(), NOW, trigger_text="你们别吵架了")
    assert factors.negative_hit == "吵架"
    assert factors.total == 0.0


def test_recent_reply_frequency_lowers_score():
    state = _state()
    state.proactive_send_times = [NOW - 60.0, NOW - 120.0]
    factors = compute_score(state, "一起打副本", _config(), NOW, trigger_text="一起打副本")
    assert factors.recent_reply_frequency == 0.3

    heavy = _state()
    heavy.proactive_send_times = [NOW - i * 60.0 for i in range(1, 7)]
    factors = compute_score(heavy, "一起打副本", _config(), NOW, trigger_text="一起打副本")
    assert factors.recent_reply_frequency == 0.1


def test_addressed_trigger_doubles_score():
    plain = compute_score(_state(), "一起打副本", _config(), NOW, trigger_text="一起打副本")
    addressed = compute_score(
        _state(), "一起打副本", _config(), NOW, trigger_text="一起打副本", addressed=True
    )
    assert plain.addressed_to_me == 1.0
    assert addressed.addressed_to_me == 2.0
    assert addressed.total > plain.total


def test_question_in_calm_chat_raises_opportunity():
    plain = compute_score(_state(), "随便聊聊", _config(), NOW, trigger_text="随便聊聊")
    question = compute_score(
        _state(), "有人知道这个怎么解决吗", _config(), NOW, trigger_text="有人知道这个怎么解决吗"
    )
    assert plain.social_opportunity == 1.15  # calm-moment bonus only
    assert question.social_opportunity == 1.5  # question + calm, capped
    assert question.question_like is True
    assert question.total > plain.total


def test_is_question_like():
    assert is_question_like("这个什么时候上线？")
    assert is_question_like("有人知道吗")
    assert not is_question_like("哈哈哈哈")


def test_busy_chat_lowers_score():
    quiet = compute_score(_state(), "一起打副本", _config(), NOW, trigger_text="一起打副本").total
    busy_state = GroupState(message_times=[NOW - 1] * 25)
    busy_state.last_user_message_time = NOW - 1.0
    busy = compute_score(busy_state, "一起打副本", _config(), NOW, trigger_text="一起打副本").total
    assert busy < quiet


def test_score_is_clamped():
    factors = compute_score(
        _state(), "副本 抽卡 副本 抽卡", _config(), NOW, trigger_text="副本 抽卡 副本 抽卡", addressed=True
    )
    assert 0.0 <= factors.total <= 3.0


def test_per_group_keyword_override():
    config = _config(group_keyword_overrides={"umo": ["钓鱼"]})
    factors = compute_score(
        _state(), "今天去钓鱼", config, NOW, trigger_text="今天去钓鱼", umo="umo"
    )
    assert "钓鱼" in factors.matched_keywords
