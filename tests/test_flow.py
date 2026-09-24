import pytest

from core.config import PluginConfig
from core.flow import FlowController, GlobalFlowState, refill_tokens
from core.models import GroupState
from core.timeutil import day_key


def _config(**overrides) -> PluginConfig:
    base = dict(
        timezone="UTC",
        group_token_bucket_capacity=3,
        group_token_refill_seconds=100,
        global_token_bucket_capacity=10,
        global_token_refill_seconds=100,
        global_daily_proactive_cap=5,
        send_failure_backoff_seconds=300,
        max_failure_backoff_seconds=3600,
    )
    base.update(overrides)
    return PluginConfig(**base)


def _has_tz(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


# -- refill ---------------------------------------------------------------


def test_refill_reaches_capacity_when_never_used():
    assert refill_tokens(0.0, 0.0, 3, 100, 50.0) == 3.0


def test_refill_is_partial_and_capped():
    assert refill_tokens(0.0, 100.0, 3, 100, 150.0) == pytest.approx(0.5)
    assert refill_tokens(2.0, 100.0, 3, 100, 9999.0) == 3.0


# -- buckets --------------------------------------------------------------


def test_group_bucket_exhausts_and_refills():
    controller = FlowController(_config())
    state = GroupState()
    for _ in range(3):
        assert controller.check(state, 1000.0)[0] is True
        controller.reserve(state, 1000.0)

    allowed, reason = controller.check(state, 1000.0)
    assert allowed is False
    assert reason == "group_bucket"

    # +100s refills exactly one token.
    assert controller.check(state, 1100.0)[0] is True


def test_global_bucket_is_shared_across_groups():
    controller = FlowController(
        _config(group_token_bucket_capacity=0, global_token_bucket_capacity=2)
    )
    group_a, group_b = GroupState(), GroupState()
    controller.reserve(group_a, 1000.0)
    controller.reserve(group_b, 1000.0)

    allowed, reason = controller.check(group_b, 1000.0)
    assert allowed is False
    assert reason == "global_bucket"


def test_global_daily_cap_blocks_after_successes():
    controller = FlowController(_config(global_daily_proactive_cap=2))
    state = GroupState()
    for _ in range(2):
        controller.reserve(state, 1000.0)
        controller.commit_success(state, 1000.0)

    allowed, reason = controller.check(state, 1000.0)
    assert allowed is False
    assert reason == "global_daily_cap"


def test_rollback_refunds_tokens_and_commit_is_not_counted_on_failure():
    controller = FlowController(_config())
    state = GroupState()
    controller.reserve(state, 1000.0)
    assert state.group_tokens == 2.0

    controller.rollback(state, 1000.0)
    assert state.group_tokens == 3.0
    assert controller.global_state.proactive_sent_today == 0


# -- backoff --------------------------------------------------------------


def test_failure_backoff_grows_exponentially_and_caps():
    controller = FlowController(
        _config(send_failure_backoff_seconds=100, max_failure_backoff_seconds=250)
    )
    state = GroupState()

    controller.rollback(state, 1000.0)
    assert state.failure_count == 1
    assert state.send_blocked_until == 1100.0

    controller.rollback(state, 2000.0)
    assert state.send_blocked_until == 2200.0

    controller.rollback(state, 3000.0)
    assert state.send_blocked_until == 3250.0  # 100 * 2**2 = 400, capped to 250


def test_failure_backoff_blocks_then_clears():
    controller = FlowController(
        _config(send_failure_backoff_seconds=100, max_failure_backoff_seconds=100)
    )
    state = GroupState()
    controller.rollback(state, 1000.0)

    assert controller.check(state, 1050.0) == (False, "failure_backoff")
    assert controller.check(state, 1100.0)[0] is True


def test_commit_success_resets_backoff():
    controller = FlowController(_config())
    state = GroupState(failure_count=2, send_blocked_until=5000.0)
    controller.commit_success(state, 1000.0)
    assert state.failure_count == 0
    assert state.send_blocked_until == 0.0


# -- timezone -------------------------------------------------------------


def test_day_key_uses_timezone():
    if not _has_tz("Asia/Shanghai") or not _has_tz("UTC"):
        pytest.skip("tzdata not available")
    # 2023-12-31 20:00 UTC == 2024-01-01 04:00 in Shanghai.
    timestamp = 1704052800.0
    assert day_key(timestamp, "Asia/Shanghai") == "2024-01-01"
    assert day_key(timestamp, "UTC") == "2023-12-31"


def test_global_daily_resets_on_new_day():
    if not _has_tz("Asia/Shanghai"):
        pytest.skip("tzdata not available")
    controller = FlowController(
        _config(timezone="Asia/Shanghai", global_daily_proactive_cap=1)
    )
    state = GroupState()
    day_one = 1704067200.0  # 2024-01-01 08:00 Shanghai
    day_two = day_one + 86400

    controller.reserve(state, day_one)
    controller.commit_success(state, day_one)
    assert controller.global_state.proactive_sent_today == 1
    assert controller.check(state, day_one) == (False, "global_daily_cap")

    allowed, _ = controller.check(state, day_two)
    assert allowed is True
    assert controller.global_state.proactive_sent_today == 0


# -- global state serialisation ------------------------------------------


def test_global_state_roundtrip_and_malformed_fields():
    state = GlobalFlowState.from_dict(
        {"proactive_sent_today": "3", "bucket_tokens": "1.5", "junk": 1}
    )
    assert state.proactive_sent_today == 3
    assert state.bucket_tokens == 1.5

    fallback = GlobalFlowState.from_dict({"proactive_sent_today": "abc"})
    assert fallback.proactive_sent_today == 0


def test_load_global_replaces_state():
    controller = FlowController(_config())
    controller.load_global({"proactive_sent_today": 7, "daily_reset_date": "2026-01-01"})
    assert controller.global_state.proactive_sent_today == 7
