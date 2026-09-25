"""Default-config speak-probability sanity checks.

Run with ``pytest tests/test_speak_probability.py -s`` to print the funnel
table and the one-hour simulation report. The assertions exist to keep the
tuning honest: if someone re-tightens (or blows open) the probability
funnel, these bounds should fail loudly.

The simulation uses the real engine with default pacing values; only the
debounce is shortened to keep wall-clock time feasible (debounce length
affects how often rounds fire, not the per-round probability). The fake
decision model always answers SPEAK, so the send counts are an upper bound —
a real model answers IGNORE/WAIT most of the time on top of this.
"""

import asyncio
import json
import random

from core.config import PluginConfig
from core.engine import SocialEngine
from core.models import GroupState
from core.scorer import compute_score

UMO = "aiocqhttp:GroupMessage:1"
NOW = 1000.0

DEFAULTS = PluginConfig(active_hours="")

PLAIN_TEXTS = [
    "今天天气不错",
    "早高峰堵车堵麻了",
    "中午吃什么好呢",
    "下午的会议好长",
    "晚上一起去跑步吗",
]
KEYWORD_TEXTS = [
    "周末打个副本吧",
    "我抽卡出货了",
]


def _single_round_probability(
    state: GroupState, text: str, *, addressed: bool = False
) -> tuple[float, float]:
    breakdown = compute_score(
        state, text, DEFAULTS, NOW, addressed=addressed, trigger_text=text
    )
    probability = min(DEFAULTS.base_speak_probability * breakdown.total, 0.8)
    return breakdown.total, probability


def _make_state(**overrides) -> GroupState:
    energy = overrides.pop("social_energy", DEFAULTS.energy_initial)
    state = GroupState(social_energy=energy, **overrides)
    state.last_user_message_time = NOW - 5.0
    return state


def _quiet_state(**overrides) -> GroupState:
    state = _make_state(**overrides)
    state.message_times = [NOW - 5.0]  # one recent human message
    return state


def _busy_state(**overrides) -> GroupState:
    state = _make_state(**overrides)
    state.message_times = [NOW - i * 4.0 for i in range(15)]  # 15 msgs / 60s
    return state


def test_print_single_round_probability_table():
    cases = [
        ("新群安静闲聊（无关键词）", _quiet_state(), "今天天气不错", False, 0.18, 0.32),
        ("新群安静闲聊（命中1个关键词）", _quiet_state(), "周末打个副本吧", False, 0.25, 0.42),
        ("安静群有人提问（无关键词）", _quiet_state(), "有人知道这个怎么解决吗", False, 0.25, 0.42),
        ("被引用回复（addressed，应打满上限）", _quiet_state(), "你是说副本机制吗", True, 0.70, 0.81),
        ("热闹群（60秒15条，无关键词）", _busy_state(), "今天天气不错", False, 0.07, 0.15),
        ("5分钟内已发过2条", _quiet_state(proactive_send_times=[NOW - 60, NOW - 120]), "今天天气不错", False, 0.04, 0.12),
        ("能量地板0.1（连发一天后）", _quiet_state(social_energy=0.1), "今天天气不错", False, 0.0, 0.08),
    ]
    print(f"\n{'场景':<28} {'得分':>6} {'单次概率':>8}")
    for name, state, text, addressed, low, high in cases:
        score, probability = _single_round_probability(state, text, addressed=addressed)
        print(f"{name:<28} {score:>6.2f} {probability:>7.1%}")
        assert low <= probability <= high, f"{name}: prob={probability:.3f} outside [{low}, {high}]"


def _make_sim_engine(config: PluginConfig, clock: list[float], sent: list, evals: list, logs: list):
    async def llm_decide(umo, system_prompt, prompt):
        evals.append(prompt)
        return json.dumps({"action": "SPEAK", "topic": "闲聊", "reply": "这个话题有意思"})

    async def send_message(umo, text, mention_user_id=None):
        sent.append(text)
        return True

    async def no_sleep(_delay):
        return None

    return SocialEngine(
        config,
        llm_decide=llm_decide,
        send_message=send_message,
        clock=lambda: clock[0],
        sleep=no_sleep,
        rng=random.Random(20260925),
        log=logs.append,
    )


async def _run_hour(texts: list[str]) -> dict:
    # Defaults everywhere except debounce (wall-clock feasibility, see module
    # docstring) and the active-hours window (fixed fake clock).
    config = PluginConfig(
        group_allowlist=[UMO],
        debounce_seconds=0.001,
        debounce_max_wait_seconds=0.002,
        active_hours="",
    )
    clock = [0.0]
    sent: list[str] = []
    evals: list[str] = []
    logs: list[str] = []
    engine = _make_sim_engine(config, clock, sent, evals, logs)

    last_bot_id = ""
    for index, text in enumerate(texts):
        clock[0] = index * 10.0
        await engine.handle_message(
            UMO, message_id=f"m{index}", sender=f"u{index % 7}", text=text
        )
        await engine.wait_idle()
        messages = engine.get_state(UMO).messages
        if messages and messages[-1].get("is_bot"):
            last_bot_id = str(messages[-1].get("message_id", ""))

    state = engine.get_state(UMO)
    rounds = sum(1 for line in logs if "score=" in line)
    return {
        "rounds": rounds,
        "evaluations": len(evals),
        "sends": len(sent),
        "energy": state.social_energy,
        "sent_today": state.proactive_sent_today,
    }


async def _hour_of_chatter(keyword_every: int) -> list[str]:
    texts = []
    for index in range(360):  # one message every 10s for an hour
        if keyword_every and index % keyword_every == 0:
            texts.append(KEYWORD_TEXTS[index // keyword_every % len(KEYWORD_TEXTS)])
        else:
            texts.append(PLAIN_TEXTS[index % len(PLAIN_TEXTS)])
    return texts


def test_print_one_hour_simulation_report():
    plain = asyncio.run(_run_hour(asyncio.run(_hour_of_chatter(0))))
    keyword = asyncio.run(_run_hour(asyncio.run(_hour_of_chatter(5))))

    print(
        "\n一小时仿真（每10秒一条消息，决策模型总是愿意回复 = 上界估计）\n"
        f"  纯闲聊（无关键词） : 决策轮 {plain['rounds']:>3} | "
        f"LLM 评估 {plain['evaluations']:>3} | 发送 {plain['sends']:>2} 条 | "
        f"终态能量 {plain['energy']:.2f}\n"
        f"  每5条含关键词     : 决策轮 {keyword['rounds']:>3} | "
        f"LLM 评估 {keyword['evaluations']:>3} | 发送 {keyword['sends']:>2} 条 | "
        f"终态能量 {keyword['energy']:.2f}"
    )

    for name, report in (("纯闲聊", plain), ("含关键词", keyword)):
        assert 2 <= report["sends"] <= 20, f"{name}: sends={report['sends']} out of sane bounds"
        assert report["evaluations"] <= report["rounds"]
        assert report["sends"] <= report["sent_today"]
    assert keyword["sends"] >= plain["sends"], "keyword topics should speak at least as often"
    assert plain["sends"] < DEFAULTS.daily_proactive_cap + 1
