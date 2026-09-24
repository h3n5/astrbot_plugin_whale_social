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

    return SocialEngine(
        config,
        llm_decide=llm_decide,
        send_message=send_message,
        writeback=writeback or default_writeback,
        sleep=sleep or _no_sleep,
        rng=rng or FakeRng(),
        clock=clock or (lambda: 1000.0),
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
    assert len(threads[0].messages) == 4


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


