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
from .decision import (
    build_decision_prompt,
    build_reply_prompt,
    build_reply_system_prompt,
    build_system_prompt,
    parse_decision,
)
from .dedup import MessageDeduplicator
from .flow import FlowController
from .gate import check_gate
from .memory import build_group_hint
from .models import GroupState, MessageEnvelope
from .reply import sanitize_reply
from .scorer import compute_score, format_factors
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
LlmReply = Callable[[str, str, str], Awaitable[Optional[str]]]
SendMessage = Callable[[str, str, Optional[str]], Awaitable[bool]]
Writeback = Callable[[str, str, str], Awaitable[None]]
OnChange = Callable[[], None]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
Log = Callable[[str], None]

ENERGY_MENTION_BONUS = 0.10
ENERGY_KEYWORD_BONUS = 0.03
ENERGY_FLOOR = 0.1
MENTION_HOLD_SECONDS = 60.0
REPLY_MIN_DELAY = 2.0
REPLY_MAX_DELAY = 8.0
MAX_PROBABILITY = 0.8
SCORING_TAIL = 5
LOCAL_ECHO_WINDOW = 30.0


def describe_gate_reason(
    state: GroupState, config: PluginConfig, now: float, reason: str
) -> str:
    """Human-readable Chinese description of a gate rejection reason."""
    if reason == "cooldown":
        remaining = max(0, int(state.next_speak_after - now))
        return f"冷却中（还剩 {remaining}s）"
    if reason == "failure_backoff":
        remaining = max(0, int(state.send_blocked_until - now))
        return f"发送失败退避（还剩 {remaining}s）"
    if reason == "rate_limit":
        return "刷屏保护（30 秒内消息过密）"
    if reason == "consecutive_bot":
        return "刚发过言，等待回应"
    if reason == "group_bucket":
        return "本群发言令牌不足"
    if reason == "global_bucket":
        return "全局发言令牌不足"
    if reason == "global_daily_cap":
        return "全局今日额度已用完"
    if reason == "daily_cap":
        return f"本群今日额度已用完（{state.proactive_sent_today}/{config.daily_proactive_cap}）"
    if reason == "quiet_hours":
        return "静默时段"
    if reason == "disabled":
        return "插件全局关闭"
    return reason


class SocialEngine:
    def __init__(
        self,
        config: PluginConfig,
        *,
        llm_decide: LlmDecide,
        send_message: SendMessage,
        llm_reply: Optional[LlmReply] = None,
        writeback: Optional[Writeback] = None,
        on_change: Optional[OnChange] = None,
        clock: Clock = time.time,
        sleep: Sleeper = asyncio.sleep,
        rng: Optional[random.Random] = None,
        trace: Optional[Log] = None,
    ) -> None:
        self.config = config
        self.llm_decide = llm_decide
        self.llm_reply = llm_reply
        self.send_message = send_message
        self.writeback = writeback
        self.on_change = on_change
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        # Observability-only decision trace sink. None means fully off: call
        # sites guard on ``is not None`` so nothing is formatted, and a raising
        # sink must never affect decisions (see _safe_trace).
        self.trace = trace
        # Last emitted blocking-trace reason per group: repeated blocks with
        # the same reason stay silent until the reason changes.
        self._last_block_trace: dict[str, str] = {}

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
        self._last_block_trace.pop(umo, None)
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
                self._last_block_trace.pop(umo, None)
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
            # A new day refills the social battery: yesterday's sends must not
            # keep today's probability pinned near the energy floor.
            state.social_energy = self._clamp_energy(
                self.config.energy_initial, self.config.energy_max
            )

    def _record_decision(self, umo: str, outcome: str, trace_msg: str = "") -> None:
        """Set the observability outcome for this group and trace it."""
        self.last_decision[umo] = outcome
        if trace_msg:
            self._safe_trace(trace_msg)
        self._last_block_trace.pop(umo, None)

    def _safe_trace(self, message: str) -> None:
        """Emit a trace line; a raising sink must never affect decisions."""
        if self.trace is None:
            return
        try:
            self.trace(message)
        except Exception:  # pragma: no cover - sink must not break the engine
            pass

    def _trace_block(self, umo: str, reason_key: str, message: str) -> None:
        """Trace a blocking state, deduplicated by reason per group.

        100 messages arriving during one cooldown produce a single line; a
        different reason (or any non-blocking event) emits again.
        """
        if self.trace is None:
            return
        if self._last_block_trace.get(umo) == reason_key:
            return
        self._last_block_trace[umo] = reason_key
        self._safe_trace(message)

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
            if self.trace is not None:
                self._trace_block(
                    umo,
                    "mentioned",
                    f"被 @ 观察：交给默认代理回复，{int(MENTION_HOLD_SECONDS)}s 内压制主动发言",
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
            if self.trace is not None:
                self._trace_block(
                    umo,
                    f"gate:{gate.reason}",
                    f"被拦下：{describe_gate_reason(state, self.config, now, gate.reason)}",
                )
            self._notify()
            return

        self._last_block_trace.pop(umo, None)
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

    @staticmethod
    def _trigger_addresses_bot(
        state: GroupState, trigger: Optional[dict[str, Any]]
    ) -> bool:
        """Whether the trigger message talks *to* the bot (quoted a bot message).

        This feeds the ``addressed_to_me`` social factor. Direct @/wake
        mentions never reach this path: they are observe-only by design.
        """
        if trigger is None:
            return False
        reply_to = str(trigger.get("reply_to", "") or "")
        if not reply_to:
            return False
        return any(
            item.get("is_bot") and str(item.get("message_id", "")) == reply_to
            for item in state.messages
        )

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
        # Energy is a send battery: interest-topic chatter tops it up, only
        # actually speaking drains it (collector.note_outgoing), and a new day
        # refills it (_ensure_daily_reset). Ordinary chat must not ratchet it
        # toward the floor — that pinned the whole probability funnel near 0.5%
        # for groups whose topics are outside the keyword list.
        if keyword_hits(text, self.config.interest_keyword_list(umo)):
            state.social_energy = self._clamp_energy(
                state.social_energy + ENERGY_KEYWORD_BONUS, self.config.energy_max
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
            if self.trace is not None:
                self._trace_block(
                    umo, "gate:mentioned", "静默后 被拦下：刚被 @（压制中）"
                )
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
            if self.trace is not None:
                self._trace_block(
                    umo,
                    f"gate:{gate.reason}",
                    f"静默后 被拦下：{describe_gate_reason(state, self.config, now, gate.reason)}",
                )
            self._notify()
            return

        refresh_activities(state, self.config, now)
        thread = select_thread(state, self.config, now, umo=umo)
        if thread is None:
            if self.trace is not None:
                self._trace_block(umo, "no_thread", "静默后 无值得参与的会话")
            self._notify()
            return

        trigger = last_human_message(thread)
        trigger_text = str(trigger.get("text", "")) if trigger else ""
        addressed = self._trigger_addresses_bot(state, trigger)
        scored_text = " ".join(
            str(item.get("text", "")) for item in thread.messages[-SCORING_TAIL:]
        )
        factors = compute_score(
            state,
            scored_text,
            self.config,
            now,
            umo=umo,
            addressed=addressed,
            trigger_text=trigger_text,
        )
        if factors.total <= 0:
            if self.trace is not None:
                self._trace_block(
                    umo,
                    "negative",
                    f"命中负向关键词「{factors.negative_hit}」，放弃参与",
                )
            self._notify()
            return

        probability = min(
            max(self.config.base_speak_probability * factors.total, 0.0),
            MAX_PROBABILITY,
        )
        roll = self.rng.random()
        if self.trace is not None:
            breakdown = (
                f"相关性 {factors.conversation_relevance:.2f}"
                f" × 活跃 {factors.conversation_activity:.2f}"
                f" × 时机 {factors.social_opportunity:.2f}"
                f" × 频率 {factors.recent_reply_frequency:.2f}"
                f" × 能量 {factors.social_energy:.2f}"
                f" × 被回复 {factors.addressed_to_me:.2f}"
            )
            # Strictly less: a zero probability must never roll a pass.
            passed = roll < probability
            self._safe_trace(
                f"静默掷骰{'通过' if passed else '未过'}：概率 {probability:.1%} "
                f"{'≥' if passed else '<'} 点数 {roll:.1%}"
                f"｜得分 {factors.total:.2f}（{breakdown}）"
                f"｜会话 {thread.id}｜触发「{trigger_text}」"
            )
            self._last_block_trace.pop(umo, None)
        if roll >= probability:
            self._notify()
            return

        state.selected_thread_id = thread.id
        # One-shot reservation: the task remembers which thread/revision it was
        # armed for, and re-checks both right before sending.
        state.reserved_thread_id = thread.id
        state.reserved_revision = state.revision
        task = asyncio.create_task(
            self._evaluate_and_reply(
                umo, thread.id, trigger_text, state.revision, addressed
            )
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
        addressed: bool = False,
    ) -> None:
        state = self.get_state(umo)
        now = self.clock()
        if in_cooldown(state, now):
            return

        thread = find_thread(state, thread_id)
        if thread is None or not thread_is_active(thread, self.config, now):
            self._record_decision(umo, "thread_ended", f"静默后会话已结束：{thread_id}")
            return

        # Recompute the factors at evaluate time so the decision model sees
        # the social signals as of now, not as of the debounce fire.
        scored_text = " ".join(
            str(item.get("text", "")) for item in thread.messages[-SCORING_TAIL:]
        )
        factors = compute_score(
            state,
            scored_text,
            self.config,
            now,
            umo=umo,
            addressed=addressed,
            trigger_text=trigger_text,
        )
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
            factors_text=format_factors(factors),
        )

        raw = await self.llm_decide(umo, system_prompt, prompt)
        now = self.clock()
        if raw is None:
            # Provider missing / network error: distinguish from a model that
            # answered but declined, and back off so a hot group cannot hammer
            # a broken provider.
            self._note_llm_failure(state, now)
            self._record_decision(
                umo,
                "llm_failed",
                f"决策模型调用失败：退避 {max(0, int(state.next_speak_after - now))}s",
            )
            return
        decision = parse_decision(raw or "")
        if decision is None:
            # The model responded but not with valid JSON; back off as well.
            self._note_llm_failure(state, now)
            self._record_decision(
                umo,
                "parse_failed",
                f"决策模型输出无法解析为 JSON：退避 {max(0, int(state.next_speak_after - now))}s",
            )
            return

        chosen = thread
        if decision.thread_id:
            candidate = find_thread(state, decision.thread_id)
            if candidate is None or not thread_is_active(candidate, self.config, self.clock()):
                self._record_decision(
                    umo, "bad_thread", f"模型选择了无效会话 {decision.thread_id}"
                )
                return
            chosen = candidate

        topic_label = decision.topic or chosen.topic or state.shown_topic or "未识别"
        if decision.action == "IGNORE":
            # P0 fix: IGNORE is a local choice, not "the bot was ignored".
            self._record_decision(
                umo,
                "IGNORE",
                f"模型判断 IGNORE（{decision.reason or '未说明'}）｜会话 {chosen.id}｜话题 {topic_label}",
            )
            return
        if decision.action == "WAIT":
            state.selected_thread_id = chosen.id
            state.shown_topic = decision.topic or chosen.topic or state.shown_topic
            self._record_decision(
                umo,
                "WAIT",
                f"模型判断 WAIT（{decision.reason or '未说明'}）｜会话 {chosen.id}｜话题 {topic_label}",
            )
            return

        reply: Optional[str] = decision.reply
        if self.llm_reply is not None:
            # Two-stage flow: the decision model chose to speak, a separate
            # reply-generation call (persona-aware, via the adapter) writes
            # the actual line.
            reply = await self._generate_reply(
                umo, state, chosen, decision, trigger_text, addressed
            )
            if reply is None:
                now = self.clock()
                self._note_llm_failure(state, now)
                self._record_decision(
                    umo,
                    "reply_failed",
                    f"回复生成调用失败：退避 {max(0, int(state.next_speak_after - now))}s",
                )
                return

        reply = sanitize_reply(
            reply,
            self.config.blocklist(),
            max_length=self.config.max_reply_length,
        )
        if not reply:
            self._record_decision(
                umo, "empty_reply", "回复内容被过滤为空（禁用词/空白），放弃发送"
            )
            return

        state.selected_thread_id = chosen.id
        mention_user_id = self._mention_target(chosen, decision)
        chosen_len = len(chosen.messages)

        await self.sleep(self.rng.uniform(REPLY_MIN_DELAY, REPLY_MAX_DELAY))

        now = self.clock()
        if in_cooldown(state, now):
            self._record_decision(
                umo,
                "cooldown_cancel",
                f"延迟后 被拦下：冷却中（还剩 {max(0, int(state.next_speak_after - now))}s），取消发送",
            )
            return
        if now < state.mentioned_until:
            state.mentioned_until = 0.0
            self._record_decision(umo, "mentioned_cancel", "延迟期间被 @，取消发送")
            return
        if not thread_is_active(chosen, self.config, now):
            self._record_decision(umo, "thread_ended", "延迟后会话已结束，取消发送")
            return
        if self._topic_moved_away(state, chosen, expected_revision, chosen_len, umo, now):
            # New messages arrived during the delay and the group moved on:
            # never send a reply computed for a stale topic.
            self._record_decision(
                umo, "topic_changed", "延迟后话题已被新消息切走，取消发送"
            )
            return

        allowed, reason = self.flow.check(state, now)
        if not allowed:
            self._record_decision(
                umo,
                reason,
                f"延迟后 被拦下：{describe_gate_reason(state, self.config, now, reason)}，取消发送",
            )
            return

        if self.config.dry_run:
            self._record_decision(umo, "dry_run", f"演练模式：跳过发送「{reply}」")
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
            self._record_decision(
                umo,
                "send_failed",
                f"发送失败：退避 {max(0, int(state.send_blocked_until - now))}s",
            )
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
        self._record_decision(
            umo,
            "speak",
            f"发言成功「{reply}」｜今日额度 {state.proactive_sent_today}/{self.config.daily_proactive_cap}"
            f"｜冷却 {max(0, int(state.next_speak_after - sent_at))}s",
        )

        if self.config.memory_writeback and self.writeback is not None:
            try:
                await self.writeback(umo, trigger_text, reply)
            except Exception:  # pragma: no cover - memory is best-effort
                pass
        self._notify()

    async def _generate_reply(
        self,
        umo: str,
        state: GroupState,
        thread: ConversationThread,
        decision: Any,
        trigger_text: str,
        addressed: bool,
    ) -> Optional[str]:
        """Second LLM stage: write the actual line for a SPEAK decision.

        Returns ``None`` on provider failure (mapped to ``reply_failed``); an
        empty string passes through to the empty-reply handling.
        """
        system_prompt = build_reply_system_prompt(self.config)
        prompt = build_reply_prompt(
            context_text=self.collector.format_messages(thread.messages),
            trigger_text=trigger_text,
            reason=decision.reason,
            topic=decision.topic or thread.topic or state.shown_topic,
            addressed=addressed,
        )
        raw = await self.llm_reply(umo, system_prompt, prompt)
        if raw is None:
            return None
        return (raw or "").strip()

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
