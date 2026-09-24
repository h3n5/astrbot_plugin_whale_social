"""Tests for the pure debounce deadline policy."""

from core.config import PluginConfig
from core.debounce import DebounceTracker
from core.models import GroupState


def make_config(**overrides) -> PluginConfig:
    base = dict(debounce_seconds=3.0, debounce_max_wait_seconds=8.0)
    base.update(overrides)
    return PluginConfig(**base)


def test_arm_sets_first_trigger_and_deadline():
    state = GroupState()
    config = make_config()
    deadline = DebounceTracker.arm(state, config, 1000.0)
    assert deadline == 1003.0
    assert state.debounce_deadline == 1003.0
    assert state.debounce_first_trigger == 1000.0


def test_reset_extends_but_never_past_max_wait():
    state = GroupState()
    config = make_config()
    DebounceTracker.arm(state, config, 1000.0)
    assert DebounceTracker.arm(state, config, 1002.0) == 1005.0
    # First trigger + max_wait = 1008 caps the quiet extension.
    assert DebounceTracker.arm(state, config, 1007.0) == 1008.0
    assert state.debounce_first_trigger == 1000.0


def test_due_and_remaining():
    state = GroupState()
    config = make_config()
    assert not DebounceTracker.due(state, 1000.0)
    assert DebounceTracker.remaining(state, 1000.0) == 0.0
    DebounceTracker.arm(state, config, 1000.0)
    assert DebounceTracker.remaining(state, 1000.0) == 3.0
    assert not DebounceTracker.due(state, 1002.9)
    assert DebounceTracker.due(state, 1003.0)


def test_zero_max_wait_disables_cap():
    state = GroupState()
    config = make_config(debounce_max_wait_seconds=0.0)
    DebounceTracker.arm(state, config, 1000.0)
    assert DebounceTracker.arm(state, config, 1005.0) == 1008.0


def test_clear_resets_deadline():
    state = GroupState()
    config = make_config()
    DebounceTracker.arm(state, config, 1000.0)
    DebounceTracker.clear(state)
    assert state.debounce_deadline == 0.0
    assert state.debounce_first_trigger == 0.0
    assert not DebounceTracker.due(state, 1000.0)
