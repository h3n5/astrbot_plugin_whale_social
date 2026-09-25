import asyncio
import json

from core.config import PluginConfig
from core.engine import SocialEngine

UMO = "aiocqhttp:GroupMessage:123"


class FakeRng:
    def __init__(self, uniform_value: float = 0.0, random_value: float = 0.0) -> None:
        self.uniform_value = uniform_value
        self.random_value = random_value

    def uniform(self, low: float, high: float) -> float:
        return self.uniform_value

    def random(self) -> float:
        return self.random_value


async def _no_sleep(_delay: float) -> None:
    return None


def make_config(**overrides) -> PluginConfig:
    base = dict(
        group_allowlist=[UMO],
        base_speak_probability=1.0,
        active_hours="",
        interest_keywords="副本",
        min_cooldown_seconds=10.0,
        max_cooldown_seconds=10.0,
        reply_window_seconds=30.0,
        debounce_seconds=0.01,
        debounce_max_wait_seconds=0.02,
        min_thread_messages=1,
    )
    base.update(overrides)
    return PluginConfig(**base)


def make_engine(
    config,
    decision,
    *,
    sent=None,
    writebacks=None,
    rng=None,
    clock=None,
    sleep=None,
    writeback=None,
    send_result=True,
    llm_calls=None,
    log=None,
    llm_reply=None,
):
    async def llm_decide(umo, system_prompt, prompt):
        if llm_calls is not None:
            llm_calls.append(prompt)
        return decision

    async def send_message(umo, text, mention_user_id=None):
        if sent is not None:
            sent.append((umo, text, mention_user_id))
        return send_result

    async def default_writeback(umo, user_text, assistant_text):
        if writebacks is not None:
            writebacks.append((umo, user_text, assistant_text))

    async def default_llm_reply(umo, system_prompt, prompt):
        return None

    return SocialEngine(
        config,
        llm_decide=llm_decide,
        send_message=send_message,
        llm_reply=llm_reply,
        writeback=writeback or default_writeback,
        sleep=sleep or _no_sleep,
        rng=rng or FakeRng(),
        clock=clock or (lambda: 1000.0),
        log=log,
    )


def test_speak_sends_schedules_and_writes_back():
    async def scenario():
        sent, writebacks = [], []
        decision = json.dumps({"action": "SPEAK", "topic": "副本", "reply": "带我一个"})
        engine = make_engine(
            make_config(), decision, sent=sent, writebacks=writebacks, rng=FakeRng(uniform_value=10.0)
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent, writebacks

    engine, sent, writebacks = asyncio.run(scenario())
    assert sent == [(UMO, "带我一个", None)]
    state = engine.get_state(UMO)
    assert state.consecutive_bot_messages == 1
    assert state.proactive_sent_today == 1
    assert state.next_speak_after == 1010.0
    assert state.awaiting_reply_until == 1030.0
    assert writebacks == [(UMO, "今晚打副本吗", "带我一个")]
    assert engine.flow.global_state.proactive_sent_today == 1


def test_mention_is_observe_only():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(), json.dumps({"action": "SPEAK", "reply": "x"}), sent=sent
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="鲸鱼娘在吗", is_bot=False, mentioned=True
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == []
    assert engine.pending == {}
    assert engine.get_state(UMO).mentioned_until > 0


def test_ignore_does_not_mark_bot_ignored():
    async def scenario():
        engine = make_engine(make_config(), json.dumps({"action": "IGNORE", "reason": "no"}))
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine

    engine = asyncio.run(scenario())
    assert engine.get_state(UMO).last_bot_ignored is False


def test_parse_failure_sends_nothing():
    async def scenario():
        sent = []
        engine = make_engine(make_config(), "我觉得可以聊", sent=sent)
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == []
    assert engine.last_decision[UMO] == "parse_failed"


def test_duplicate_event_only_sends_once():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(), json.dumps({"action": "SPEAK", "reply": "hi"}), sent=sent
        )
        for _ in range(3):
            await engine.handle_message(
                UMO, message_id="same", sender="u", text="打副本", is_bot=False
            )
        await engine.wait_idle()
        return sent

    assert len(asyncio.run(scenario())) == 1


def test_human_message_resets_bot_streak():
    async def scenario():
        engine = make_engine(
            make_config(base_speak_probability=0.0), json.dumps({"action": "IGNORE"})
        )
        engine.get_state(UMO).consecutive_bot_messages = 3
        await engine.handle_message(
            UMO, message_id="m", sender="u", text="大家好呀", is_bot=False
        )
        await engine.wait_idle()
        return engine

    engine = asyncio.run(scenario())
    assert engine.get_state(UMO).consecutive_bot_messages == 0


def test_dry_run_does_not_send_or_mutate_state():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(dry_run=True),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            sent=sent,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == []
    assert engine.get_state(UMO).proactive_sent_today == 0


def test_blocked_reply_is_not_sent():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(output_blocklist="广告"),
            json.dumps({"action": "SPEAK", "reply": "这是广告"}),
            sent=sent,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        return sent

    assert asyncio.run(scenario()) == []


def test_cooldown_blocks_then_allows():
    async def scenario():
        sent = []
        clock_state = [1000.0]
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            sent=sent,
            rng=FakeRng(uniform_value=10.0),
            clock=lambda: clock_state[0],
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        # In cooldown: should not send.
        await engine.handle_message(
            UMO, message_id="2", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        clock_state[0] = 1011.0
        await engine.handle_message(
            UMO, message_id="3", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        return sent

    assert len(asyncio.run(scenario())) == 2


def test_mention_during_delay_cancels_reply():
    class GateSleep:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self, _delay):
            self.entered.set()
            await self.release.wait()

    async def scenario():
        sent = []
        sleeper = GateSleep()
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "topic": "副本", "reply": "hi"}),
            sent=sent,
            sleep=sleeper,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await sleeper.entered.wait()
        # A mention arrives while the reply is queued.
        await engine.handle_message(
            UMO, message_id="2", sender="u2", text="鲸鱼娘 在吗", is_bot=False, mentioned=True
        )
        sleeper.release.set()
        await engine.wait_idle()
        return sent

    assert asyncio.run(scenario()) == []


def test_failed_writeback_does_not_break_send():
    async def failing_writeback(umo, user_text, assistant_text):
        raise RuntimeError("memory down")

    async def scenario():
        sent = []
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            sent=sent,
            writeback=failing_writeback,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        return sent

    assert asyncio.run(scenario()) == [(UMO, "hi", None)]


def test_disallowed_group_is_ignored():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(group_allowlist=[]),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            sent=sent,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        return sent

    assert asyncio.run(scenario()) == []


def test_shutdown_cancels_pending_tasks():
    class GateSleep:
        def __init__(self):
            self.entered = asyncio.Event()

        async def __call__(self, _delay):
            self.entered.set()
            await asyncio.Event().wait()

    async def scenario():
        sent = []
        sleeper = GateSleep()
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            sent=sent,
            sleep=sleeper,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await sleeper.entered.wait()
        await engine.shutdown()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == []
    assert engine.pending == {}


def test_send_failure_sets_backoff_and_does_not_count():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(send_failure_backoff_seconds=100, max_failure_backoff_seconds=100),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            sent=sent,
            send_result=False,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u", text="打副本", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == [(UMO, "hi", None)]  # send was attempted
    state = engine.get_state(UMO)
    assert state.failure_count == 1
    assert state.send_blocked_until == 1100.0
    assert state.proactive_sent_today == 0
    assert engine.flow.global_state.proactive_sent_today == 0


def test_maybe_evict_drops_stale_states_by_ttl():
    engine = make_engine(
        make_config(state_ttl_seconds=1000, max_group_states=0),
        json.dumps({"action": "IGNORE"}),
    )
    engine.get_state("fresh").last_user_message_time = 4500.0
    engine.get_state("stale").last_user_message_time = 100.0

    removed = engine.maybe_evict(5000.0)
    assert removed == ["stale"]
    assert "fresh" in engine.states
    assert "stale" not in engine.states


def test_maybe_evict_caps_count_keeping_most_recent():
    engine = make_engine(
        make_config(state_ttl_seconds=0, max_group_states=2),
        json.dumps({"action": "IGNORE"}),
    )
    for group, last in (("g0", 300.0), ("g1", 100.0), ("g2", 200.0)):
        engine.get_state(group).last_user_message_time = last

    engine.maybe_evict(1000.0)
    assert set(engine.states) == {"g0", "g2"}


def test_global_flow_state_roundtrips_through_engine():
    engine = make_engine(make_config(), json.dumps({"action": "IGNORE"}))
    engine.load_global({"proactive_sent_today": 4, "daily_reset_date": "2026-01-01"})
    exported = engine.export_global()
    assert exported["proactive_sent_today"] == 4
    assert exported["daily_reset_date"] == "2026-01-01"


def test_debounce_coalesces_burst_into_single_decision():
    async def scenario():
        sent, llm_calls = [], []
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            sent=sent,
            rng=FakeRng(uniform_value=10.0),
            llm_calls=llm_calls,
        )
        for index in range(4):
            await engine.handle_message(
                UMO, message_id=f"m{index}", sender="u1", text="今晚打副本", is_bot=False
            )
        await engine.wait_idle()
        return engine, sent, llm_calls

    engine, sent, llm_calls = asyncio.run(scenario())
    assert len(llm_calls) == 1
    assert len(sent) == 1
    threads = engine.get_state(UMO).threads
    assert len(threads) == 1
    human_messages = [item for item in threads[0].messages if not item.get("is_bot")]
    assert len(human_messages) == 4
    # The bot's own send is recorded locally into the same thread.
    assert any(item.get("is_bot") for item in threads[0].messages)


def test_prompt_includes_selected_thread_id():
    async def scenario():
        llm_calls = []
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            rng=FakeRng(uniform_value=10.0),
            llm_calls=llm_calls,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本", is_bot=False
        )
        await engine.wait_idle()
        return llm_calls

    llm_calls = asyncio.run(scenario())
    assert llm_calls and "t1" in llm_calls[0]


def test_target_user_mention_sent_when_enabled():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(reply_mention_user=True),
            json.dumps(
                {"action": "SPEAK", "target": {"type": "USER", "user_id": "u1"}, "reply": "hi"}
            ),
            sent=sent,
            rng=FakeRng(uniform_value=10.0),
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本", is_bot=False
        )
        await engine.wait_idle()
        return sent

    assert asyncio.run(scenario()) == [(UMO, "hi", "u1")]


def test_target_user_mention_disabled_by_default():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(),
            json.dumps(
                {"action": "SPEAK", "target": {"type": "USER", "user_id": "u1"}, "reply": "hi"}
            ),
            sent=sent,
            rng=FakeRng(uniform_value=10.0),
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本", is_bot=False
        )
        await engine.wait_idle()
        return sent

    assert asyncio.run(scenario()) == [(UMO, "hi", None)]


def test_bad_thread_id_downgrades_to_no_reply():
    async def scenario():
        sent = []
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "thread_id": "ghost", "reply": "hi"}),
            sent=sent,
            rng=FakeRng(uniform_value=10.0),
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == []
    assert engine.last_decision[UMO] == "bad_thread"


def test_bot_reply_attaches_to_selected_thread():
    async def scenario():
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "hi"}),
            rng=FakeRng(uniform_value=10.0),
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本", is_bot=False
        )
        await engine.wait_idle()
        await engine.handle_message(
            UMO, message_id="2", sender="9999", text="来了", is_bot=True
        )
        return engine

    engine = asyncio.run(scenario())
    threads = engine.get_state(UMO).threads
    assert len(threads) == 1
    assert threads[0].bot_participated is True
    assert any(item.get("is_bot") for item in threads[0].messages)
    assert "9999" not in threads[0].participants


def test_reconnect_replay_deduped_beyond_collector_backlog():
    async def scenario():
        engine = make_engine(
            make_config(),
            json.dumps({"action": "IGNORE"}),
        )
        for index in range(250):
            await engine.handle_message(
                UMO, message_id=f"m{index}", sender="u1", text="打副本", is_bot=False
            )
        # Replay the very first event; collector's 200-entry backstop has already
        # evicted it, so only the global TTL deduplicator can catch it.
        await engine.handle_message(
            UMO, message_id="m0", sender="u1", text="打副本", is_bot=False
        )
        return engine

    engine = asyncio.run(scenario())
    assert engine.deduplicator.duplicates == 1


def test_idless_messages_are_not_deduped_by_default():
    async def scenario():
        engine = make_engine(make_config(), json.dumps({"action": "IGNORE"}))
        for _ in range(2):
            await engine.handle_message(
                UMO, message_id="", sender="u1", text="哈哈", is_bot=False
            )
        return engine

    engine = asyncio.run(scenario())
    assert len(engine.get_state(UMO).messages) == 2
    assert engine.deduplicator.duplicates == 0


def test_idless_fallback_dedups_when_enabled():
    async def scenario():
        engine = make_engine(
            make_config(dedup_fallback_seconds=10.0),
            json.dumps({"action": "IGNORE"}),
        )
        for _ in range(2):
            await engine.handle_message(
                UMO, message_id="", sender="u1", text="哈哈", is_bot=False
            )
        return engine

    engine = asyncio.run(scenario())
    assert len(engine.get_state(UMO).messages) == 1
    assert engine.deduplicator.duplicates == 1


def test_phase_reports_idle_then_waiting():
    async def scenario():
        engine = make_engine(make_config(), json.dumps({"action": "IGNORE"}))
        assert engine.phase(UMO) == "idle"
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="打副本", is_bot=False
        )
        waiting = engine.phase(UMO)
        await engine.wait_idle()
        return engine, waiting

    engine, waiting = asyncio.run(scenario())
    assert waiting == "waiting"
    assert engine.phase(UMO) == "idle"


class _GateSleep:
    """Reply-delay sleeper the test can hold and release."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, _delay: float) -> None:
        self.entered.set()
        await self.release.wait()


def test_topic_switch_during_delay_cancels_stale_reply():
    async def scenario():
        sent = []
        sleeper = _GateSleep()
        engine = make_engine(
            make_config(interest_keywords="副本\n抽卡"),
            json.dumps({"action": "SPEAK", "topic": "副本", "reply": "hi"}),
            sent=sent,
            sleep=sleeper,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await sleeper.entered.wait()
        # The group switches to another topic while the reply is queued.
        await engine.handle_message(
            UMO, message_id="2", sender="u2", text="新卡池抽卡吗", is_bot=False
        )
        await engine.handle_message(
            UMO, message_id="3", sender="u3", text="抽卡出货了", is_bot=False
        )
        sleeper.release.set()
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == []
    assert engine.last_decision[UMO] == "topic_changed"


def test_same_thread_progress_during_delay_still_sends():
    async def scenario():
        sent = []
        sleeper = _GateSleep()
        engine = make_engine(
            make_config(interest_keywords="副本\n抽卡"),
            json.dumps({"action": "SPEAK", "topic": "副本", "reply": "hi"}),
            sent=sent,
            sleep=sleeper,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await sleeper.entered.wait()
        # Someone continues the *same* thread: the reply is still timely.
        await engine.handle_message(
            UMO, message_id="2", sender="u2", text="副本我也去", is_bot=False
        )
        sleeper.release.set()
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == [(UMO, "hi", None)]


def test_quiet_hours_use_configured_timezone():
    import core.engine as engine_mod

    calls = []
    original = engine_mod.local_datetime

    def fake_local(now, tz_name):
        calls.append(tz_name)
        return original(now, tz_name)

    engine_mod.local_datetime = fake_local
    try:
        async def scenario():
            engine = make_engine(
                make_config(timezone="Asia/Shanghai"), json.dumps({"action": "IGNORE"})
            )
            await engine.handle_message(
                UMO, message_id="1", sender="u1", text="打副本", is_bot=False
            )
            await engine.wait_idle()

        asyncio.run(scenario())
    finally:
        engine_mod.local_datetime = original
    assert calls and all(name == "Asia/Shanghai" for name in calls)


def test_unrelated_chatter_keeps_reply_window_open():
    async def scenario():
        engine = make_engine(
            make_config(base_speak_probability=0.0), json.dumps({"action": "IGNORE"})
        )
        state = engine.get_state(UMO)
        state.awaiting_reply_until = 2000.0
        state.last_bot_ignored = False
        await engine.handle_message(
            UMO, message_id="1", sender="u2", text="今天天气不错", is_bot=False
        )
        await engine.wait_idle()
        return state

    state = asyncio.run(scenario())
    assert state.awaiting_reply_until == 2000.0
    assert state.last_bot_ignored is False


def test_thread_continuation_closes_reply_window():
    async def scenario():
        engine = make_engine(
            make_config(interest_keywords="副本"),
            json.dumps({"action": "SPEAK", "topic": "副本", "reply": "hi"}),
            rng=FakeRng(uniform_value=10.0),
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        state = engine.get_state(UMO)
        assert state.awaiting_reply_until == 1030.0
        await engine.handle_message(
            UMO, message_id="2", sender="u2", text="副本我也去", is_bot=False
        )
        await engine.wait_idle()
        return state

    state = asyncio.run(scenario())
    assert state.awaiting_reply_until == 0.0
    assert state.last_bot_ignored is False


def test_reply_to_bot_message_closes_reply_window():
    async def scenario():
        engine = make_engine(
            make_config(base_speak_probability=0.0), json.dumps({"action": "IGNORE"})
        )
        state = engine.get_state(UMO)
        state.awaiting_reply_until = 2000.0
        state.messages.append(
            {
                "message_id": "botmsg",
                "sender": "9999",
                "text": "hi",
                "timestamp": 1000.0,
                "is_bot": True,
                "kind": "text",
                "reply_to": "",
                "at_users": [],
            }
        )
        await engine.handle_message(
            UMO, message_id="m", sender="u1", text="你说啥", is_bot=False, reply_to="botmsg"
        )
        await engine.wait_idle()
        return state

    state = asyncio.run(scenario())
    assert state.awaiting_reply_until == 0.0


def test_successful_send_records_outgoing_in_thread():
    async def scenario():
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "带我一个"}),
            rng=FakeRng(uniform_value=10.0),
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine

    engine = asyncio.run(scenario())
    state = engine.get_state(UMO)
    assert len(state.threads) == 1
    assert state.threads[0].bot_participated is True
    assert any(
        item.get("is_bot") and item.get("text") == "带我一个"
        for item in state.threads[0].messages
    )
    assert any(
        item.get("is_bot") and item.get("text") == "带我一个" for item in state.messages
    )


def test_platform_echo_of_own_send_is_not_double_counted():
    async def scenario():
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "reply": "带我一个"}),
            rng=FakeRng(uniform_value=10.0),
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        state = engine.get_state(UMO)
        streak_before = state.consecutive_bot_messages
        await engine.handle_message(
            UMO, message_id="echo1", sender="9999", text="带我一个", is_bot=True
        )
        return state, streak_before

    state, streak_before = asyncio.run(scenario())
    assert streak_before == 1
    assert state.consecutive_bot_messages == 1
    assert (
        sum(
            1
            for item in state.messages
            if item.get("is_bot") and item.get("text") == "带我一个"
        )
        == 1
    )


def test_echo_arriving_during_send_is_suppressed_and_marker_cleared():
    async def scenario():
        holder = {}
        sent = []
        decision = json.dumps({"action": "SPEAK", "reply": "带我一个"})

        async def llm_decide(umo, system_prompt, prompt):
            return decision

        async def send_message(umo, text, mention_user_id=None):
            sent.append((umo, text, mention_user_id))
            # Simulate the platform echoing our message back before send returns.
            await holder["engine"].handle_message(
                umo, message_id="echo-now", sender="9999", text=text, is_bot=True
            )
            return True

        engine = SocialEngine(
            make_config(),
            llm_decide=llm_decide,
            send_message=send_message,
            sleep=_no_sleep,
            rng=FakeRng(uniform_value=10.0),
            clock=lambda: 1000.0,
        )
        holder["engine"] = engine
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert len(sent) == 1
    state = engine.get_state(UMO)
    assert state.consecutive_bot_messages == 1
    assert state.local_outgoing_at == 0.0
    assert sum(1 for item in state.messages if item.get("is_bot")) == 1


def test_llm_call_failure_sets_backoff_and_metric():
    async def scenario():
        sent = []
        engine = make_engine(make_config(), None, sent=sent)
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    assert sent == []
    assert engine.last_decision[UMO] == "llm_failed"
    state = engine.get_state(UMO)
    assert state.llm_failure_count == 1
    assert state.next_speak_after == 1060.0


def test_parse_failure_sets_backoff_and_metric():
    async def scenario():
        engine = make_engine(make_config(), "我觉得可以聊")
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine

    engine = asyncio.run(scenario())
    assert engine.last_decision[UMO] == "parse_failed"
    state = engine.get_state(UMO)
    assert state.llm_failure_count == 1
    assert state.next_speak_after == 1060.0




def test_debounce_fire_skips_when_decision_pending():
    async def scenario():
        llm_calls = []
        engine = make_engine(
            make_config(), json.dumps({"action": "SPEAK", "reply": "hi"}), llm_calls=llm_calls
        )
        hold = asyncio.create_task(asyncio.sleep(3600))
        engine.pending[UMO] = hold
        try:
            await engine._on_debounce_fire(UMO)
        finally:
            hold.cancel()
        await engine.wait_idle()
        return engine, llm_calls

    engine, llm_calls = asyncio.run(scenario())
    assert llm_calls == []
    assert engine.states == {}


def test_maybe_evict_clears_last_decision():
    engine = make_engine(make_config(state_ttl_seconds=10), json.dumps({"action": "IGNORE"}))
    engine.get_state(UMO).last_user_message_time = 1000.0
    engine.last_decision[UMO] = "speak"

    removed = engine.maybe_evict(2000.0)
    assert removed == [UMO]
    assert UMO not in engine.states
    assert UMO not in engine.last_decision


def test_reset_group_clears_last_decision():
    engine = make_engine(make_config(), json.dumps({"action": "IGNORE"}))
    engine.get_state(UMO)
    engine.last_decision[UMO] = "speak"

    engine.reset_group(UMO)
    assert UMO not in engine.states
    assert UMO not in engine.last_decision


def test_reply_truncated_to_configured_max_length():
    async def scenario():
        sent = []
        decision = json.dumps({"action": "SPEAK", "reply": "这个副本好难呀打得我头都秃了"})
        engine = make_engine(make_config(max_reply_length=5), decision, sent=sent)
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return sent

    sent = asyncio.run(scenario())
    assert sent == [(UMO, "这个副本好", None)]


def test_decision_funnel_logs_score_and_result():
    async def scenario():
        logs = []
        engine = make_engine(
            make_config(),
            json.dumps({"action": "SPEAK", "topic": "闲聊", "reply": "聊得热闹"}),
            log=logs.append,
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="随便聊聊最近怎么样", is_bot=False
        )
        await engine.wait_idle()
        return logs

    logs = asyncio.run(scenario())
    assert any("score=" in line and "prob=" in line and "roll=" in line for line in logs)
    assert any("-> speak" in line for line in logs)


def test_daily_reset_refills_social_energy():
    engine = make_engine(make_config(), json.dumps({"action": "IGNORE"}))
    state = engine.get_state(UMO)
    state.social_energy = 0.1
    state.proactive_sent_today = 7
    state.daily_reset_date = "2000-01-01"

    engine._ensure_daily_reset(state, 1000.0)
    assert state.social_energy == engine.config.energy_initial
    assert state.proactive_sent_today == 0


def test_plain_chat_no_longer_drains_energy():
    async def scenario():
        engine = make_engine(make_config(), json.dumps({"action": "IGNORE"}))
        for index in range(5):
            await engine.handle_message(
                UMO,
                message_id=f"m{index}",
                sender="u1",
                text="普通闲聊消息",
                is_bot=False,
            )
            await engine.wait_idle()
        return engine

    engine = asyncio.run(scenario())
    assert engine.get_state(UMO).social_energy == engine.config.energy_initial


def test_speak_uses_reply_stage_for_text_and_prompt_has_factors():
    async def scenario():
        llm_calls, reply_calls, sent = [], [], []

        async def llm_reply(umo, system_prompt, prompt):
            reply_calls.append(prompt)
            return "新生成的回复"

        decision = json.dumps({"action": "SPEAK", "topic": "副本", "reply": "旧字段回复"})
        engine = make_engine(
            make_config(), decision, sent=sent, llm_calls=llm_calls, llm_reply=llm_reply
        )
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return llm_calls, reply_calls, sent

    llm_calls, reply_calls, sent = asyncio.run(scenario())
    assert "本地社交信号" in llm_calls[0]
    assert "addressed_to_me" in llm_calls[0]
    assert reply_calls and "请直接输出要发送的那条群聊消息" in reply_calls[0]
    assert sent == [(UMO, "新生成的回复", None)]


def test_reply_stage_failure_records_reply_failed_and_backs_off():
    async def scenario():
        sent = []

        async def llm_reply(umo, system_prompt, prompt):
            return None

        decision = json.dumps({"action": "SPEAK", "reply": "x"})
        engine = make_engine(make_config(), decision, sent=sent, llm_reply=llm_reply)
        await engine.handle_message(
            UMO, message_id="1", sender="u1", text="今晚打副本吗", is_bot=False
        )
        await engine.wait_idle()
        return engine, sent

    engine, sent = asyncio.run(scenario())
    state = engine.get_state(UMO)
    assert sent == []
    assert engine.last_decision[UMO] == "reply_failed"
    assert state.llm_failure_count == 1
    assert state.next_speak_after > 0


def test_addressed_reply_to_bot_message_boosts_factors():
    async def scenario():
        llm_calls = []
        decision = json.dumps({"action": "SPEAK", "reply": "对，就是这样"})
        engine = make_engine(make_config(), decision, sent=[], llm_calls=llm_calls)
        # Seed a bot message the trigger can quote.
        await engine.handle_message(
            UMO, message_id="b1", sender="bot", text="我昨晚过了那个副本", is_bot=True
        )
        await engine.handle_message(
            UMO,
            message_id="2",
            sender="u1",
            text="你说的是什么副本",
            is_bot=False,
            reply_to="b1",
        )
        await engine.wait_idle()
        return llm_calls

    llm_calls = asyncio.run(scenario())
    assert llm_calls
    assert '"addressed_to_me": 2.0' in llm_calls[-1]
