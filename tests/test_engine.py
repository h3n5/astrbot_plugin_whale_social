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
):
    async def llm_decide(umo, system_prompt, prompt):
        return decision

    async def send_message(umo, text):
        if sent is not None:
            sent.append((umo, text))
        return True

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
    assert sent == [(UMO, "带我一个")]
    state = engine.get_state(UMO)
    assert state.consecutive_bot_messages == 1
    assert state.proactive_sent_today == 1
    assert state.next_speak_after == 1010.0
    assert state.awaiting_reply_until == 1030.0
    assert writebacks == [(UMO, "今晚打副本吗", "带我一个")]


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

    assert asyncio.run(scenario()) == [(UMO, "hi")]


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
