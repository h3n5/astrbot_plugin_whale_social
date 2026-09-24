"""Tests for conversation thread clustering, activity and selection."""

from core.config import PluginConfig
from core.models import ChatMessage, GroupState
from core.threads import (
    ConversationThread,
    assign_thread,
    find_thread,
    format_overview,
    last_human_message,
    prune_threads,
    refresh_activities,
    select_thread,
    thread_is_active,
)


def make_config(**overrides) -> PluginConfig:
    base = dict(
        interest_keywords="副本\n抽卡\n攻略",
        thread_window_seconds=180.0,
        thread_join_time_gap_seconds=120.0,
        max_threads=3,
        min_thread_messages=1,
        thread_selection="most_active",
    )
    base.update(overrides)
    return PluginConfig(**base)


def msg(mid, sender, text, ts, *, is_bot=False, reply_to="", at_users=()):
    return ChatMessage(
        message_id=mid,
        sender=sender,
        text=text,
        timestamp=ts,
        is_bot=is_bot,
        reply_to=reply_to,
        at_users=list(at_users),
    ).to_dict()


def test_first_message_creates_thread():
    state = GroupState()
    config = make_config()
    thread = assign_thread(state, msg("1", "u1", "今晚打副本吗", 1000.0), config, 1000.0)
    assert thread.id == "t1"
    assert state.thread_seq == 1
    assert thread.participants == ["u1"]
    assert len(thread.messages) == 1
    assert thread.topic == "副本"


def test_keyword_overlap_joins_thread():
    state = GroupState()
    config = make_config()
    first = assign_thread(state, msg("1", "u1", "今晚打副本吗", 1000.0), config, 1000.0)
    second = assign_thread(state, msg("2", "u2", "副本我可以", 1010.0), config, 1010.0)
    assert second is first
    assert len(state.threads) == 1


def test_different_topics_get_separate_threads():
    state = GroupState()
    config = make_config()
    assign_thread(state, msg("1", "u1", "今晚打副本吗", 1000.0), config, 1000.0)
    assign_thread(state, msg("2", "u2", "新卡池抽卡吗", 1005.0), config, 1005.0)
    assert len(state.threads) == 2


def test_participant_continuation_joins_thread():
    state = GroupState()
    config = make_config()
    first = assign_thread(state, msg("1", "u1", "今天天气不错", 1000.0), config, 1000.0)
    second = assign_thread(state, msg("2", "u1", "是啊真舒服", 1005.0), config, 1005.0)
    assert second is first
    assert len(state.threads) == 1


def test_reply_relation_joins_thread():
    state = GroupState()
    config = make_config()
    first = assign_thread(state, msg("1", "u1", "今晚打副本吗", 1000.0), config, 1000.0)
    joined = assign_thread(
        state, msg("2", "u2", "我也去", 1010.0, reply_to="1"), config, 1010.0
    )
    assert joined is first
    assert len(state.threads) == 1


def test_at_relation_joins_targets_thread():
    state = GroupState()
    config = make_config()
    a = assign_thread(state, msg("1", "u1", "今晚打副本吗", 1000.0), config, 1000.0)
    assign_thread(state, msg("2", "u2", "新卡池抽卡吗", 1005.0), config, 1005.0)
    joined = assign_thread(
        state, msg("3", "u3", "带我一个", 1010.0, at_users=["u1"]), config, 1010.0
    )
    assert joined is a


def test_prune_ends_stale_threads():
    state = GroupState()
    config = make_config()
    assign_thread(state, msg("1", "u1", "副本", 1000.0), config, 1000.0)
    prune_threads(state, config, 1000.0 + config.thread_window_seconds + 1)
    assert all(thread.ended for thread in state.threads)


def test_prune_caps_threads_keeping_most_active():
    state = GroupState()
    config = make_config(max_threads=2, thread_window_seconds=0.0)
    for index in range(4):
        thread = ConversationThread(
            id=f"t{index}",
            last_activity=1000.0 + index,
            created_at=1000.0 + index,
        )
        thread.participants = ["u1"]
        for i in range(index + 1):
            thread.messages.append(msg(f"{index}-{i}", "u1", "副本", 1000.0 + index))
        state.threads.append(thread)
    refresh_activities(state, config, 1005.0)
    prune_threads(state, config, 1005.0)
    active_ids = {thread.id for thread in state.threads if not thread.ended}
    assert active_ids == {"t3", "t2"}


def test_select_prefers_most_active():
    state = GroupState()
    config = make_config()
    for index, (tid, count) in enumerate((("a", 1), ("b", 3))):
        thread = ConversationThread(id=tid, last_activity=1000.0)
        thread.participants = [f"u{index}"]
        for i in range(count):
            thread.messages.append(msg(f"{tid}-{i}", f"u{index}", "闲聊", 1000.0))
        state.threads.append(thread)
    refresh_activities(state, config, 1000.0)
    selected = select_thread(state, config, 1000.0)
    assert selected is not None and selected.id == "b"


def test_select_interest_mode_prefers_keyword_thread():
    state = GroupState()
    config = make_config(thread_selection="interest")
    hot = ConversationThread(id="hot", last_activity=1000.0)
    hot.participants = ["u1"]
    for i in range(3):
        hot.messages.append(msg(f"hot-{i}", "u1", "闲聊", 1000.0))
    interesting = ConversationThread(id="interesting", last_activity=1000.0)
    interesting.participants = ["u2"]
    interesting.messages.append(msg("i-0", "u2", "抽卡出货了", 1000.0))
    state.threads.extend([hot, interesting])
    refresh_activities(state, config, 1000.0)
    selected = select_thread(state, config, 1000.0)
    assert selected is not None and selected.id == "interesting"


def test_bot_participation_boosts_activity():
    state = GroupState()
    config = make_config()
    for index, (tid, bot) in enumerate((("a", False), ("b", True))):
        thread = ConversationThread(id=tid, last_activity=1000.0, bot_participated=bot)
        thread.participants = [f"u{index}"]
        thread.messages.append(msg(f"{tid}-0", f"u{index}", "闲聊", 1000.0))
        state.threads.append(thread)
    refresh_activities(state, config, 1000.0)
    selected = select_thread(state, config, 1000.0)
    assert selected is not None and selected.id == "b"


def test_select_respects_min_thread_messages():
    state = GroupState()
    config = make_config(min_thread_messages=2)
    thread = ConversationThread(id="a", last_activity=1000.0)
    thread.participants = ["u1"]
    thread.messages.append(msg("a-0", "u1", "副本", 1000.0))
    state.threads.append(thread)
    assert select_thread(state, config, 1000.0) is None


def test_format_overview_excludes_selected():
    state = GroupState()
    config = make_config()
    first = ConversationThread(id="a", last_activity=1000.0, topic="副本")
    first.participants = ["u1"]
    first.messages.append(msg("a-0", "u1", "副本", 1000.0))
    second = ConversationThread(id="b", last_activity=999.0, topic="抽卡")
    second.participants = ["u2"]
    second.messages.append(msg("b-0", "u2", "抽卡", 999.0))
    state.threads.extend([first, second])
    refresh_activities(state, config, 1000.0)
    overview = format_overview(state, config, 1000.0, exclude_id="a")
    assert "b" in overview
    assert "\na" not in overview


def test_find_and_last_human_helpers():
    state = GroupState()
    config = make_config()
    thread = assign_thread(state, msg("1", "u1", "副本", 1000.0), config, 1000.0)
    thread.messages.append(msg("2", "bot", "来了", 1001.0, is_bot=True))
    assert find_thread(state, thread.id) is thread
    assert find_thread(state, "missing") is None
    assert last_human_message(thread)["message_id"] == "1"
    assert thread_is_active(thread, config, 1000.0)
    assert not thread_is_active(thread, config, 1000.0 + config.thread_window_seconds + 1)
