import pytest

from core import cooldown
from core.config import PluginConfig
from core.models import GroupState


class FakeRng:
    def __init__(self, value: float) -> None:
        self.value = value
        self.bounds: tuple[float, float] | None = None

    def uniform(self, low: float, high: float) -> float:
        self.bounds = (low, high)
        return self.value


def _config(**overrides) -> PluginConfig:
    base = dict(min_cooldown_seconds=100.0, max_cooldown_seconds=200.0)
    base.update(overrides)
    return PluginConfig(**base)


def test_normal_tier_uses_configured_range():
    state = GroupState()
    rng = FakeRng(150.0)
    assert cooldown.select_tier(state) == "normal"
    assert cooldown.next_cooldown_seconds(state, _config(), rng) == 150.0
    assert rng.bounds == (100.0, 200.0)


def test_recent_tier_shortens_cooldown():
    state = GroupState(last_bot_message_time=500.0)
    rng = FakeRng(0.0)
    assert cooldown.select_tier(state) == "recent"
    cooldown.next_cooldown_seconds(state, _config(), rng)
    assert rng.bounds == pytest.approx((100.0 / 3.0, 100.0))


def test_consecutive_tier_doubles_cooldown():
    state = GroupState(consecutive_bot_messages=1)
    rng = FakeRng(0.0)
    assert cooldown.select_tier(state) == "consecutive"
    cooldown.next_cooldown_seconds(state, _config(), rng)
    assert rng.bounds == (200.0, 400.0)


def test_ignored_tier_is_strongest():
    state = GroupState(last_bot_ignored=True)
    rng = FakeRng(0.0)
    assert cooldown.select_tier(state) == "ignored"
    cooldown.next_cooldown_seconds(state, _config(), rng)
    assert rng.bounds == (200.0, 600.0)


def test_schedule_next_speak_persists_deadline_once():
    state = GroupState()
    deadline = cooldown.schedule_next_speak(state, _config(), 1000.0, FakeRng(50.0))
    assert deadline == 1050.0
    assert state.next_speak_after == 1050.0


def test_reply_window_settles_to_ignored():
    state = GroupState()
    cooldown.open_reply_window(state, _config(reply_window_seconds=30.0), 100.0)
    assert state.awaiting_reply_until == 130.0
    assert state.last_bot_ignored is False

    assert cooldown.settle_reply_window(state, 129.0) is False
    assert cooldown.settle_reply_window(state, 131.0) is True
    assert state.last_bot_ignored is True
    assert state.awaiting_reply_until == 0.0
    # Settling again is a no-op.
    assert cooldown.settle_reply_window(state, 200.0) is False


def test_human_reply_closes_window_before_it_expires():
    state = GroupState()
    cooldown.open_reply_window(state, _config(reply_window_seconds=30.0), 100.0)
    assert cooldown.note_human_reply(state, 110.0) is True
    assert state.awaiting_reply_until == 0.0
    assert cooldown.note_human_reply(state, 120.0) is False


def test_in_cooldown_boundaries():
    state = GroupState(next_speak_after=100.0)
    assert cooldown.in_cooldown(state, 99.9) is True
    assert cooldown.in_cooldown(state, 100.0) is False
    assert cooldown.in_cooldown(GroupState(), 1.0) is False
