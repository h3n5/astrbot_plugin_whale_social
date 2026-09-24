"""Social engine: orchestration of collect -> gate -> score -> LLM -> reply.

All AstrBot interaction is injected as async callables so the engine can be
exercised in tests with fakes.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Optional

from core.collector import MessageCollector
from core.config import PluginConfig
from core.cooldown import in_cooldown, settle_reply_window
from core.decision import build_decision_prompt, build_system_prompt, parse_decision
from core.flow import FlowController
from core.gate import check_gate
from core.memory import build_group_hint
from core.models import GroupState
from core.reply import sanitize_reply
from core.scorer import compute_score
from core.timeutil import day_key
from core.topic import keyword_hits

LlmDecide = Callable[[str, str, str], Awaitable[Optional[str]]]
SendMessage = Callable[[str, str], Awaitable[bool]]
Writeback = Callable[[str, str, str], Awaitable[None]]
OnChange = Callable[[], None]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

ENERGY_MENTION_BONUS = 0.10
ENERGY_KEYWORD_BONUS = 0.03
ENERGY_CHAT_PENALTY = 0.005
ENERGY_FLOOR = 0.1
MENTION_HOLD_SECONDS = 60.0
REPLY_MIN_DELAY = 2.0
REPLY_MAX_DELAY = 8.0
REPLY_DELAY_HORIZON = 5.0


class SocialEngine:
    def __init__(
        self,
        config: PluginConfig,
        *,
        llm_decide: LlmDecide,
        send_message: SendMessage,
        writeback: Optional[Writeback] = None,
        on_change: Optional[OnChange] = None,
        clock: Clock = time.time,
        sleep: Sleeper = asyncio.sleep,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.config = config
        self.llm_decide = llm_decide
        self.send_message = send_message
        self.writeback = writeback
        self.on_change = on_change
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()

        self.states: dict[str, GroupState] = {}
        self.collector = MessageCollector(config)
        self.flow = FlowController(config)
        self.pending: dict[str, asyncio.Task[None]] = {}
        self.last_decision: dict[str, str] = {}

    # -- state helpers ---------------------------------------------------

    def get_state(self, umo: str) -> GroupState:
        state = self.states.get(umo)
        if state is None:
            state = GroupState(social_energy=self.config.energy_initial)
            self.states[umo] = state
        return state

    def set_state(self, umo: str, state: GroupState) -> None:
        self.states[umo] = state

    def is_allowed_group(self, umo: str) -> bool:
        return bool(self.config.enabled) and umo in set(self.config.group_allowlist)

    def enable_group(self, umo: str) -> bool:
        if umo in self.config.group_allowlist:
            return False
        self.config.group_allowlist.append(umo)
        self._notify()
        return True

    def disable_group(self, umo: str) -> bool:
        if umo not in self.config.group_allowlist:
            return False
        self.config.group_allowlist.remove(umo)
        self._notify()
        return True

    def reset_group(self, umo: str) -> None:
        if umo in self.states:
            del self.states[umo]
        self._notify()

    def export_persist(self) -> dict[str, dict[str, Any]]:
        return {umo: state.to_persist_dict() for umo, state in self.states.items()}

    def load_persist(self, data: Optional[dict[str, dict[str, Any]]]) -> None:
        for umo, payload in (data or {}).items():
            self.states[str(umo)] = GroupState.from_persist_dict(
                payload,
                energy_initial=self.config.energy_initial,
            )

    def export_global(self) -> dict[str, Any]:
        return self.flow.export_global()

    def load_global(self, data: Optional[dict[str, Any]]) -> None:
        self.flow.load_global(data)

    def maybe_evict(self, now: float) -> list[str]:
        """Drop stale groups (TTL) and cap the number of tracked groups.

        Returns the removed UMOs. Keeps the most recently active groups.
        """
        removed: list[str] = []
        ttl = self.config.state_ttl_seconds
        if ttl > 0:
            for umo, state in list(self.states.items()):
                last_seen = max(state.last_user_message_time, state.last_bot_message_time)
                if last_seen > 0 and now - last_seen > ttl:
                    del self.states[umo]
                    removed.append(umo)

        cap = self.config.max_group_states
        if cap > 0 and len(self.states) > cap:
            ordered = sorted(
                self.states.items(),
                key=lambda item: max(
                    item[1].last_user_message_time, item[1].last_bot_message_time
                ),
                reverse=True,
            )
            for umo, _state in ordered[cap:]:
                del self.states[umo]
                removed.append(umo)
        if removed:
            self._notify()
        return removed

    def _notify(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:  # pragma: no cover - callback must never break flow
            pass

    def _ensure_daily_reset(self, state: GroupState, now: float) -> None:
        today = day_key(now, self.config.timezone)
        if state.daily_reset_date != today:
            state.daily_reset_date = today
            state.proactive_sent_today = 0

    @staticmethod
    def _clamp_energy(value: float, ceiling: float) -> float:
        return max(ENERGY_FLOOR, min(value, ceiling))

    # -- main entry point ------------------------------------------------

    async def handle_message(
        self,
        umo: str,
        *,
        message_id: str,
        sender: str,
        text: str,
        is_bot: bool = False,
        kind: str = "text",
        mentioned: bool = False,
    ) -> None:
        """Observe one message. Never raises; never replies to a mention."""
        if not self.config.enabled or not self.is_allowed_group(umo):
            return

        now = self.clock()
        state = self.get_state(umo)
        self._ensure_daily_reset(state, now)
        settle_reply_window(state, now)

        recorded = self.collector.record(
            state,
            message_id=message_id,
            sender=sender,
            text=text,
            is_bot=is_bot,
            kind=kind,
            now=now,
        )
        if recorded is None:
            return  # duplicate event

        if is_bot:
            self._notify()
            return

        self._update_energy(state, text, umo)

        if mentioned:
            # Observe-only: the default agent handles actual mentions. We only
            # note that a mention happened so a queued proactive reply backs off.
            state.mentioned_until = now + MENTION_HOLD_SECONDS
            state.social_energy = self._clamp_energy(
                state.social_energy + ENERGY_MENTION_BONUS, self.config.energy_max
            )
            self._notify()
            return

        if len((text or "").strip()) < self.config.min_message_length:
            self._notify()
            return

        if umo in self.pending:
            return  # a decision for this group is already in flight

        gate = check_gate(state, self.config, now, datetime.fromtimestamp(now), flow=self.flow)
        if not gate.allowed:
            self._notify()
            return

        breakdown = compute_score(state, text, self.config, now, umo=umo)
        if breakdown.total <= 0:
            self._notify()
            return

        probability = min(
            max(self.config.base_speak_probability * breakdown.total, 0.0),
            0.8,
        )
        if self.rng.random() > probability:
            self._notify()
            return

        task = asyncio.create_task(self._evaluate_and_reply(umo, text))
        self.pending[umo] = task
        task.add_done_callback(self._done_callback(umo, task))
        self._notify()

    def _done_callback(self, umo: str, task: "asyncio.Task[None]"):
        def _callback(_task: "asyncio.Task[None]") -> None:
            # P0 fix: only clear our own task, never a newer one for the group.
            if self.pending.get(umo) is task:
                self.pending.pop(umo, None)
            self._notify()

        return _callback

    def _update_energy(self, state: GroupState, text: str, umo: str) -> None:
        hits = keyword_hits(text, self.config.interest_keyword_list(umo))
        delta = ENERGY_KEYWORD_BONUS if hits else -ENERGY_CHAT_PENALTY
        state.social_energy = self._clamp_energy(
            state.social_energy + delta, self.config.energy_max
        )

    async def _evaluate_and_reply(self, umo: str, trigger_text: str) -> None:
        state = self.get_state(umo)
        if in_cooldown(state, self.clock()):
            return

        hint = build_group_hint(state, self.config, self.clock())
        system_prompt = build_system_prompt(self.config)
        prompt = build_decision_prompt(
            context_text=self.collector.build_context(state),
            trigger_text=trigger_text,
            hint=hint,
        )

        raw = await self.llm_decide(umo, system_prompt, prompt)
        decision = parse_decision(raw or "")
        if decision is None:
            self.last_decision[umo] = "parse_failed"
            return

        self.last_decision[umo] = decision.action

        if decision.action == "IGNORE":
            # P0 fix: IGNORE is a local choice, not "the bot was ignored".
            return
        if decision.action == "WAIT":
            state.shown_topic = decision.topic or state.shown_topic
            return

        reply = sanitize_reply(decision.reply, self.config.blocklist())
        if not reply:
            self.last_decision[umo] = "empty_reply"
            return

        await self.sleep(self.rng.uniform(REPLY_MIN_DELAY, REPLY_MAX_DELAY))

        now = self.clock()
        if in_cooldown(state, now):
            self.last_decision[umo] = "cooldown_cancel"
            return
        if now < state.mentioned_until:
            state.mentioned_until = 0.0
            self.last_decision[umo] = "mentioned_cancel"
            return
        if not self._topic_still_relevant(state, umo, decision.topic):
            self.last_decision[umo] = "topic_changed"
            return

        allowed, reason = self.flow.check(state, now)
        if not allowed:
            self.last_decision[umo] = reason
            return

        if self.config.dry_run:
            self.last_decision[umo] = "dry_run"
            return

        self.flow.reserve(state, now)
        sent = await self.send_message(umo, reply)
        if not sent:
            self.flow.rollback(state, now)
            self.last_decision[umo] = "send_failed"
            self._notify()
            return
        self.flow.commit_success(state, now)

        state.mentioned_until = 0.0
        self.collector.note_outgoing(
            state, self.config, reply, now=self.clock(), rng=self.rng
        )
        self.last_decision[umo] = "speak"

        if self.config.memory_writeback and self.writeback is not None:
            try:
                await self.writeback(umo, trigger_text, reply)
            except Exception:  # pragma: no cover - memory is best-effort
                pass
        self._notify()

    def _topic_still_relevant(self, state: GroupState, umo: str, topic: str) -> bool:
        """Re-check after the reply delay that the topic did not move on.

        Only enforced when there is enough recent traffic to judge; a quiet
        group should not cancel a perfectly good reply.
        """
        recent = [
            str(item.get("text", ""))
            for item in state.messages[-int(REPLY_DELAY_HORIZON):]
            if not item.get("is_bot")
        ]
        if len(recent) < 2:
            return True
        keywords = self.config.interest_keyword_list(umo)
        if keyword_hits(topic, keywords):
            return True
        return bool(keyword_hits(" ".join(recent), keywords))

    # -- lifecycle -------------------------------------------------------

    async def wait_idle(self) -> None:
        tasks = [task for task in self.pending.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        tasks = [task for task in self.pending.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
