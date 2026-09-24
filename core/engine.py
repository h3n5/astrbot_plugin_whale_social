"""Social engine: orchestration of collect -> debounce -> thread select -> LLM -> reply.

All AstrBot interaction is injected as async callables so the engine can be
exercised in tests with fakes.

V2.0 flow: every observed message is clustered into a conversation thread. A
burst of activity in a group arms a per-group debounce; when the group falls
quiet (or the max wait elapses) the engine selects one thread and asks the LLM
whether to join it.
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
from core.debounce import DebounceTracker
from core.decision import build_decision_prompt, build_system_prompt, parse_decision
from core.flow import FlowController
from core.gate import check_gate
from core.memory import build_group_hint
from core.models import GroupState
from core.reply import sanitize_reply
from core.scorer import compute_score
from core.threads import (
    assign_thread,
    attach_bot_message,
    find_thread,
    format_overview,
    last_human_message,
    most_recent_thread,
    prune_threads,
    refresh_activities,
    select_thread,
    thread_is_active,
)
from core.timeutil import day_key
from core.topic import keyword_hits

LlmDecide = Callable[[str, str, str], Awaitable[Optional[str]]]
SendMessage = Callable[[str, str, Optional[str]], Awaitable[bool]]
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
MAX_PROBABILITY = 0.8
SCORING_TAIL = 5


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
        self.debounce_tasks: dict[str, asyncio.Task[None]] = {}
        self.last_decision: dict[str, str] = {}
        self._debounce_events: dict[str, asyncio.Event] = {}
        self._closed = False

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
        self._cancel_group_tasks(umo)
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
                    self._cancel_group_tasks(umo)
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
                self._cancel_group_tasks(umo)
                del self.states[umo]
                removed.append(umo)
        if removed:
            self._notify()
        return removed

    def _cancel_group_tasks(self, umo: str) -> None:
        for task in (self.pending.get(umo), self.debounce_tasks.get(umo)):
            if task is not None and not task.done():
                task.cancel()
        self.pending.pop(umo, None)
        self.debounce_tasks.pop(umo, None)
        self._debounce_events.pop(umo, None)

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
        reply_to: str = "",
        at_users: Optional[list[str]] = None,
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
            reply_to=reply_to,
            at_users=at_users,
        )
        if recorded is None:
            return  # duplicate event

        if not is_bot:
            state.revision += 1
        self._track_thread(state, recorded, is_bot=is_bot, umo=umo, now=now)

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

        self._arm_debounce(umo, now)
        self._notify()

    def _track_thread(
        self,
        state: GroupState,
        message: dict[str, Any],
        *,
        is_bot: bool,
        umo: str,
        now: float,
    ) -> None:
        if is_bot:
            thread = find_thread(state, state.selected_thread_id) or most_recent_thread(
                state, self.config, now
            )
            if thread is not None:
                attach_bot_message(thread, message, self.config, now)
            return
        assign_thread(state, message, self.config, now, umo=umo)
        prune_threads(state, self.config, now)

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

    # -- debounce --------------------------------------------------------

    def _arm_debounce(self, umo: str, now: float) -> None:
        if self._closed:
            return
        state = self.get_state(umo)
        DebounceTracker.arm(state, self.config, now)
        event = self._debounce_events.get(umo)
        if event is None:
            self._debounce_events[umo] = asyncio.Event()
            self.debounce_tasks[umo] = asyncio.create_task(self._debounce_worker(umo))
        else:
            event.set()

    async def _debounce_worker(self, umo: str) -> None:
        state = self.get_state(umo)
        try:
            while True:
                now = self.clock()
                remaining = DebounceTracker.remaining(state, now)
                if remaining <= 0:
                    break
                event = self._debounce_events.get(umo)
                if event is None:
                    break
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
        finally:
            self._debounce_events.pop(umo, None)
            self.debounce_tasks.pop(umo, None)
            DebounceTracker.clear(state)
        await self._on_debounce_fire(umo)

    async def _on_debounce_fire(self, umo: str) -> None:
        """Group has settled: pick one thread and maybe start a decision."""
        if self._closed:
            return
        state = self.get_state(umo)
        now = self.clock()

        if now < state.mentioned_until:
            state.mentioned_until = 0.0
            self._notify()
            return

        gate = check_gate(state, self.config, now, datetime.fromtimestamp(now), flow=self.flow)
        if not gate.allowed:
            self._notify()
            return

        refresh_activities(state, self.config, now)
        thread = select_thread(state, self.config, now, umo=umo)
        if thread is None:
            self._notify()
            return

        trigger = last_human_message(thread)
        trigger_text = str(trigger.get("text", "")) if trigger else ""
        scored_text = " ".join(
            str(item.get("text", "")) for item in thread.messages[-SCORING_TAIL:]
        )
        breakdown = compute_score(state, scored_text, self.config, now, umo=umo)
        if breakdown.total <= 0:
            self._notify()
            return

        probability = min(
            max(self.config.base_speak_probability * breakdown.total, 0.0),
            MAX_PROBABILITY,
        )
        if self.rng.random() > probability:
            self._notify()
            return

        state.selected_thread_id = thread.id
        task = asyncio.create_task(self._evaluate_and_reply(umo, thread.id, trigger_text))
        self.pending[umo] = task
        task.add_done_callback(self._done_callback(umo, task))
        self._notify()

    # -- decision + reply ------------------------------------------------

    async def _evaluate_and_reply(self, umo: str, thread_id: str, trigger_text: str) -> None:
        state = self.get_state(umo)
        now = self.clock()
        if in_cooldown(state, now):
            return

        thread = find_thread(state, thread_id)
        if thread is None or not thread_is_active(thread, self.config, now):
            self.last_decision[umo] = "thread_ended"
            return

        hint = build_group_hint(state, self.config, now)
        system_prompt = build_system_prompt(self.config)
        prompt = build_decision_prompt(
            context_text=self.collector.format_messages(thread.messages),
            trigger_text=trigger_text,
            hint=hint,
            thread_id=thread.id,
            threads_overview=format_overview(
                state, self.config, now, exclude_id=thread.id
            ),
        )

        raw = await self.llm_decide(umo, system_prompt, prompt)
        decision = parse_decision(raw or "")
        if decision is None:
            self.last_decision[umo] = "parse_failed"
            return

        chosen = thread
        if decision.thread_id:
            candidate = find_thread(state, decision.thread_id)
            if candidate is None or not thread_is_active(candidate, self.config, self.clock()):
                self.last_decision[umo] = "bad_thread"
                return
            chosen = candidate

        self.last_decision[umo] = decision.action

        if decision.action == "IGNORE":
            # P0 fix: IGNORE is a local choice, not "the bot was ignored".
            return
        if decision.action == "WAIT":
            state.selected_thread_id = chosen.id
            state.shown_topic = decision.topic or chosen.topic or state.shown_topic
            return

        reply = sanitize_reply(decision.reply, self.config.blocklist())
        if not reply:
            self.last_decision[umo] = "empty_reply"
            return

        state.selected_thread_id = chosen.id
        mention_user_id = self._mention_target(chosen, decision)

        await self.sleep(self.rng.uniform(REPLY_MIN_DELAY, REPLY_MAX_DELAY))

        now = self.clock()
        if in_cooldown(state, now):
            self.last_decision[umo] = "cooldown_cancel"
            return
        if now < state.mentioned_until:
            state.mentioned_until = 0.0
            self.last_decision[umo] = "mentioned_cancel"
            return
        if not thread_is_active(chosen, self.config, now):
            self.last_decision[umo] = "thread_ended"
            return

        allowed, reason = self.flow.check(state, now)
        if not allowed:
            self.last_decision[umo] = reason
            return

        if self.config.dry_run:
            self.last_decision[umo] = "dry_run"
            return

        self.flow.reserve(state, now)
        sent = await self.send_message(umo, reply, mention_user_id)
        if not sent:
            self.flow.rollback(state, now)
            self.last_decision[umo] = "send_failed"
            self._notify()
            return
        self.flow.commit_success(state, now)

        state.mentioned_until = 0.0
        state.shown_topic = decision.topic or chosen.topic or state.shown_topic
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

    def _mention_target(self, thread, decision) -> Optional[str]:
        if decision.target_type != "USER" or not decision.target_user_id:
            return None
        if not self.config.reply_mention_user:
            return None
        if decision.target_user_id not in thread.participants:
            return None
        return decision.target_user_id

    # -- lifecycle -------------------------------------------------------

    async def wait_idle(self) -> None:
        for _ in range(1000):
            tasks = [
                task
                for task in list(self.debounce_tasks.values()) + list(self.pending.values())
                if not task.done()
            ]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        self._closed = True
        tasks = [
            task
            for task in list(self.debounce_tasks.values()) + list(self.pending.values())
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.debounce_tasks.clear()
        self.pending.clear()
        self._debounce_events.clear()
