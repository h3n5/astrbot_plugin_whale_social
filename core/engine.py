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
from typing import Any, Optional

from .collector import MessageCollector
from .config import PluginConfig
from .cooldown import in_cooldown, note_human_reply, settle_reply_window
from .debounce import DebounceTracker
from .decision import build_decision_prompt, build_system_prompt, parse_decision
from .dedup import MessageDeduplicator
from .flow import FlowController
from .gate import check_gate
from .memory import build_group_hint
from .models import GroupState, MessageEnvelope
from .reply import sanitize_reply
from .scorer import compute_score
from .threads import (
    ConversationThread,
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
from .timeutil import day_key, local_datetime
from .topic import keyword_hits

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
LOCAL_ECHO_WINDOW = 30.0


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
        self.deduplicator = MessageDeduplicator(
            ttl_seconds=config.dedup_ttl_seconds,
            max_entries=config.dedup_max_entries,
        )
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
        # The allowlist is tiny; a plain membership check beats building a set
        # on every observed message.
        return bool(self.config.enabled) and umo in self.config.group_allowlist

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
        self.last_decision.pop(umo, None)
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
            for umo in removed:
                self.last_decision.pop(umo, None)
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
        """Observe one message. Never raises; never replies to a mention.

        The first gate is reconnect-replay de-duplication: a repeated event
        must not reach the collector, threads or the LLM.
        """
        if not self.config.enabled or not self.is_allowed_group(umo):
            return

        now = self.clock()
        envelope = MessageEnvelope(
            umo=umo,
            message_id=message_id,
            sender=sender,
            text=text,
            timestamp=now,
            is_bot=is_bot,
            kind=kind,
            mentioned=mentioned,
            reply_to=reply_to,
            at_users=list(at_users or []),
        )
        if self._is_duplicate(envelope, now):
            return

        state = self.get_state(umo)
        self._ensure_daily_reset(state, now)
        settle_reply_window(state, now)

        if is_bot and self._consume_local_echo(state, envelope):
            # The platform echoed a message we already recorded locally.
            return

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
            close_reply_window=False,
        )
        if recorded is None:
            return  # duplicate event

        if not is_bot:
            state.revision += 1
        thread = self._track_thread(state, recorded, is_bot=is_bot, umo=umo, now=now)

        if is_bot:
            self._notify()
            return

        if self._message_is_bot_related(state, envelope, thread):
            note_human_reply(state, now)

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

        gate = check_gate(
            state,
            self.config,
            now,
            local_datetime(now, self.config.timezone),
            flow=self.flow,
        )
        if not gate.allowed:
            self._notify()
            return

        self._arm_debounce(umo, now)
        self._notify()

    def _consume_local_echo(self, state: GroupState, envelope: MessageEnvelope) -> bool:
        """True when a bot message is just the platform echo of our own send.

        We record outgoing messages locally, so an echoed copy of the same text
        shortly after the send must not be counted twice. One-shot: the marker
        is cleared on match.
        """
        if state.local_outgoing_at <= 0:
            return False
        if envelope.timestamp - state.local_outgoing_at > LOCAL_ECHO_WINDOW:
            state.local_outgoing_at = 0.0
            return False
        if (envelope.text or "").strip() != (state.local_outgoing_text or "").strip():
            return False
        state.local_outgoing_at = 0.0
        return True

    def _message_is_bot_related(
        self,
        state: GroupState,
        envelope: MessageEnvelope,
        thread: Optional[ConversationThread],
    ) -> bool:
        """Whether a human message plausibly answers the bot.

        Unrelated chatter must not close the reply window / clear the
        "ignored" flag, otherwise the bot looks replied-to and becomes more
        eager.
        """
        if envelope.mentioned:
            return True
        if envelope.reply_to:
            for item in state.messages:
                if item.get("is_bot") and str(item.get("message_id", "")) == envelope.reply_to:
                    return True
        if thread is not None and (
            thread.bot_participated or thread.id == state.selected_thread_id
        ):
            return True
        return False

    def _is_duplicate(self, envelope: MessageEnvelope, now: float) -> bool:
        key = envelope.dedup_key()
        if key:
            return self.deduplicator.check_and_add(key, now)
        # No stable id: only use the coarse fingerprint when explicitly enabled.
        fingerprint = envelope.fingerprint(self.config.dedup_fallback_seconds)
        if fingerprint:
            return self.deduplicator.check_and_add(fingerprint, now)
        return False

    def _track_thread(
        self,
        state: GroupState,
        message: dict[str, Any],
        *,
        is_bot: bool,
        umo: str,
        now: float,
    ) -> Optional[ConversationThread]:
        if is_bot:
            thread = find_thread(state, state.selected_thread_id) or most_recent_thread(
                state, self.config, now
            )
            if thread is not None:
                attach_bot_message(thread, message, self.config, now)
            return thread
        thread = assign_thread(state, message, self.config, now, umo=umo)
        prune_threads(state, self.config, now)
        return thread

    def _done_callback(self, umo: str, task: "asyncio.Task[None]"):
        def _callback(_task: "asyncio.Task[None]") -> None:
            # P0 fix: only clear our own task, never a newer one for the group.
            if self.pending.get(umo) is task:
                self.pending.pop(umo, None)
                state = self.states.get(umo)
                if state is not None:
                    state.reserved_thread_id = ""
                    state.reserved_revision = 0
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
        if umo in self.pending:
            # Defense in depth: a decision is already in flight; never start a
            # second concurrent one for the same group.
            return
        state = self.get_state(umo)
        now = self.clock()

        if now < state.mentioned_until:
            state.mentioned_until = 0.0
            self._notify()
            return

        gate = check_gate(
            state,
            self.config,
            now,
            local_datetime(now, self.config.timezone),
            flow=self.flow,
        )
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
        # One-shot reservation: the task remembers which thread/revision it was
        # armed for, and re-checks both right before sending.
        state.reserved_thread_id = thread.id
        state.reserved_revision = state.revision
        task = asyncio.create_task(
            self._evaluate_and_reply(umo, thread.id, trigger_text, state.revision)
        )
        self.pending[umo] = task
        task.add_done_callback(self._done_callback(umo, task))
        self._notify()

    # -- decision + reply ------------------------------------------------

    async def _evaluate_and_reply(
        self,
        umo: str,
        thread_id: str,
        trigger_text: str,
        expected_revision: int,
    ) -> None:
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
        now = self.clock()
        if raw is None:
            # Provider missing / network error: distinguish from a model that
            # answered but declined, and back off so a hot group cannot hammer
            # a broken provider.
            self._note_llm_failure(state, now)
            self.last_decision[umo] = "llm_failed"
            return
        decision = parse_decision(raw or "")
        if decision is None:
            # The model responded but not with valid JSON; back off as well.
            self._note_llm_failure(state, now)
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

        reply = sanitize_reply(
            decision.reply,
            self.config.blocklist(),
            max_length=self.config.max_reply_length,
        )
        if not reply:
            self.last_decision[umo] = "empty_reply"
            return

        state.selected_thread_id = chosen.id
        mention_user_id = self._mention_target(chosen, decision)
        chosen_len = len(chosen.messages)

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
        if self._topic_moved_away(state, chosen, expected_revision, chosen_len, umo, now):
            # New messages arrived during the delay and the group moved on:
            # never send a reply computed for a stale topic.
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
        sent_at = self.clock()
        # Mark the intent before awaiting send so a fast platform echo of our
        # own message cannot be recorded twice.
        state.local_outgoing_at = sent_at
        state.local_outgoing_text = reply
        sent = await self.send_message(umo, reply, mention_user_id)
        if not sent:
            state.local_outgoing_at = 0.0
            state.local_outgoing_text = ""
            self.flow.rollback(state, now)
            self.last_decision[umo] = "send_failed"
            self._notify()
            return
        self.flow.commit_success(state, now)

        state.mentioned_until = 0.0
        state.shown_topic = decision.topic or chosen.topic or state.shown_topic
        marker_armed = state.local_outgoing_at > 0
        payload = self.collector.note_outgoing(
            state, self.config, reply, now=sent_at, rng=self.rng
        )
        if not marker_armed:
            # The echo already arrived during send and consumed the marker; do
            # not leave a stale one armed for an unrelated future message.
            state.local_outgoing_at = 0.0
            state.local_outgoing_text = ""
        # Local record keeps the bot's participation visible even when the
        # platform does not echo our own outbound event back.
        attach_bot_message(chosen, payload, self.config, sent_at)
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

    def _topic_moved_away(
        self,
        state: GroupState,
        chosen: ConversationThread,
        expected_revision: int,
        chosen_len: int,
        umo: str,
        now: float,
    ) -> bool:
        """True when the delay let the group move on from the chosen thread.

        If the chosen thread itself advanced, the reply is still timely. If the
        revision changed without it advancing, re-select: when another thread
        overtook it, cancel the stale reply.
        """
        if state.revision == expected_revision:
            return False
        if len(chosen.messages) > chosen_len:
            return False
        refresh_activities(state, self.config, now)
        current = select_thread(state, self.config, now, umo=umo)
        return current is None or current.id != chosen.id

    def _note_llm_failure(self, state: GroupState, now: float) -> None:
        state.llm_failure_count += 1
        backoff = max(0.0, float(self.config.llm_failure_backoff_seconds))
        if backoff > 0:
            state.next_speak_after = max(state.next_speak_after, now + backoff)

    # -- lifecycle -------------------------------------------------------

    def phase(self, umo: str) -> str:
        """Coarse task state for observability: idle/waiting/sending/cooldown.

        Mirrors the NONE -> WAITING -> THINKING/SENDING -> COOLDOWN machine;
        ``pending`` covers both thinking and sending because the engine holds a
        single in-flight decision per group.
        """
        if umo in self.pending:
            return "sending"
        if umo in self.debounce_tasks:
            return "waiting"
        state = self.states.get(umo)
        if state is not None and in_cooldown(state, self.clock()):
            return "cooldown"
        return "idle"

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
