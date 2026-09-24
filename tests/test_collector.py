from core.collector import MessageCollector
from core.config import PluginConfig
from core.models import GroupState


class FakeRng:
    def uniform(self, low: float, high: float) -> float:
        return high


def _collector(**overrides) -> MessageCollector:
    return MessageCollector(PluginConfig(**overrides))


def test_duplicate_message_id_is_ignored():
    collector = _collector()
    state = GroupState()
    first = collector.record(
        state, message_id="m1", sender="u", text="hi", is_bot=False, kind="text", now=1.0
    )
    second = collector.record(
        state, message_id="m1", sender="u", text="hi", is_bot=False, kind="text", now=1.5
    )
    assert first is not None
    assert second is None
    assert len(state.messages) == 1


def test_context_window_is_truncated():
    collector = _collector(context_message_limit=3)
    state = GroupState()
    for index in range(6):
        collector.record(
            state,
            message_id=f"m{index}",
            sender="u",
            text=f"msg{index}",
            is_bot=False,
            kind="text",
            now=float(index),
        )
    assert [item["text"] for item in state.messages] == ["msg3", "msg4", "msg5"]


def test_human_message_resets_bot_streak():
    collector = _collector()
    state = GroupState(consecutive_bot_messages=4)
    collector.record(
        state, message_id="m1", sender="u", text="hello", is_bot=False, kind="text", now=10.0
    )
    assert state.consecutive_bot_messages == 0


def test_bot_message_increases_streak():
    collector = _collector()
    state = GroupState()
    collector.record(
        state, message_id="m1", sender="bot", text="hi", is_bot=True, kind="text", now=10.0
    )
    assert state.consecutive_bot_messages == 1
    assert state.last_bot_message_time == 10.0


def test_human_message_inside_window_closes_it():
    collector = _collector(reply_window_seconds=30.0)
    state = GroupState(awaiting_reply_until=100.0)
    collector.record(
        state, message_id="m1", sender="u", text="reply", is_bot=False, kind="text", now=50.0
    )
    assert state.awaiting_reply_until == 0.0


def test_rate_buckets_prune_old_entries():
    collector = _collector()
    state = GroupState()
    collector.record(
        state, message_id="old", sender="u", text="x", is_bot=False, kind="text", now=0.0
    )
    collector.record(
        state, message_id="new", sender="u", text="y", is_bot=False, kind="text", now=500.0
    )
    assert state.message_times == [500.0]


def test_note_outgoing_updates_all_bookkeeping():
    collector = _collector(
        min_cooldown_seconds=10.0,
        max_cooldown_seconds=20.0,
        reply_window_seconds=30.0,
    )
    state = GroupState(social_energy=0.5)
    collector.note_outgoing(state, PluginConfig(min_cooldown_seconds=10.0, max_cooldown_seconds=20.0, reply_window_seconds=30.0), "hello", now=100.0, rng=FakeRng())

    assert state.last_proactive_msg == "hello"
    assert state.next_speak_after == 120.0
    assert state.consecutive_bot_messages == 1
    assert state.awaiting_reply_until == 130.0
    assert state.social_energy < 0.5
    assert state.proactive_sent_today == 1


def test_build_context_marks_bot_messages():
    collector = _collector()
    state = GroupState()
    collector.record(
        state, message_id="m1", sender="alice", text="hi", is_bot=False, kind="text", now=1000.0
    )
    collector.record(
        state, message_id="m2", sender="bot", text="hello", is_bot=True, kind="text", now=1001.0
    )
    context = collector.build_context(state)
    assert "alice" in context
    assert "[鲸鱼娘]" in context


def test_record_keeps_reply_and_at_metadata():
    collector = _collector()
    state = GroupState()
    payload = collector.record(
        state,
        message_id="m1",
        sender="u",
        text="hi",
        is_bot=False,
        kind="text",
        now=1.0,
        reply_to="p1",
        at_users=["42", "43"],
    )
    assert payload is not None
    assert payload["reply_to"] == "p1"
    assert payload["at_users"] == ["42", "43"]
    assert state.messages[-1] is payload


def test_format_messages_handles_empty_and_non_text():
    collector = _collector()
    assert collector.format_messages([]) == ""
    rendered = collector.format_messages(
        [{"sender": "u", "text": "", "kind": "image", "timestamp": 0.0}]
    )
    assert "[image]" in rendered

