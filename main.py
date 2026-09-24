"""AstrBot entry point for the whale-social proactive layer.

The handler here is intentionally thin: it only translates AstrBot events and
config into calls on :class:`core.engine.SocialEngine`. All policy lives in
``core/``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from core.config import PluginConfig
from core.content import normalize_content
from core.engine import SocialEngine
from core.memory import build_group_hint, build_writeback_user_text
from core.timeutil import local_datetime
from storage.state_store import StateStore

PLUGIN_NAME = "astrbot_plugin_whale_social"
# Hard cap on one proactive send; a hung platform adapter must not keep the
# group's decision slot occupied forever. The engine rolls back and backs off
# on failure, so a late-delivered message is the safe direction.
SEND_TIMEOUT_SECONDS = 30.0


class WhaleSocialPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.raw_config = config
        self.cfg = PluginConfig.from_mapping(config)
        self.data_dir = Path(__file__).resolve().parent / "data"
        self.store = StateStore(self.data_dir / "state.json")
        self.engine = SocialEngine(
            self.cfg,
            llm_decide=self._llm_decide,
            send_message=self._send_message,
            writeback=self._memory_writeback,
            on_change=self._schedule_save,
        )
        self._save_task: Optional[asyncio.Task[None]] = None
        self._dirty = False

    # -- lifecycle -------------------------------------------------------

    async def initialize(self) -> None:
        try:
            self.engine.load_persist(self.store.load())
            self.engine.load_global(self.store.load_global())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[{PLUGIN_NAME}] load state failed: {exc}")
        logger.info(
            f"[{PLUGIN_NAME}] started; enabled groups: "
            f"{self.cfg.group_allowlist or '（空，默认全部关闭）'}"
        )

    async def terminate(self) -> None:
        try:
            await self.engine.shutdown()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[{PLUGIN_NAME}] engine shutdown failed: {exc}")
        await self._save_now()
        logger.info(f"[{PLUGIN_NAME}] stopped")

    # -- persistence -----------------------------------------------------

    def _schedule_save(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._dirty = True
        if self._save_task is not None and not self._save_task.done():
            return
        self._save_task = loop.create_task(self._save_loop())

    async def _save_loop(self) -> None:
        # Drain the dirty flag: changes made while a save is in flight must not
        # be lost, so keep saving until one full pass observes no new change.
        while self._dirty:
            self._dirty = False
            await self._save_now()

    async def _save_now(self) -> None:
        try:
            self.engine.maybe_evict(time.time())
            await asyncio.to_thread(
                self.store.save,
                self.engine.export_persist(),
                self.engine.export_global(),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[{PLUGIN_NAME}] save state failed: {exc}")

    # -- AstrBot adapters ------------------------------------------------

    async def _resolve_provider_id(self, umo: str) -> Optional[str]:
        if self.cfg.provider_id:
            return self.cfg.provider_id
        try:
            return await self.context.get_current_chat_provider_id(umo=umo)
        except Exception:
            return None

    async def _llm_decide(self, umo: str, system_prompt: str, prompt: str) -> Optional[str]:
        provider_id = await self._resolve_provider_id(umo)
        if not provider_id:
            logger.warning(f"[{PLUGIN_NAME}] no chat provider for {umo}")
            return None
        timeout = self.cfg.llm_timeout_seconds

        async def _generate(**kwargs: Any) -> Any:
            coro = self.context.llm_generate(**kwargs)
            if timeout > 0:
                # A hung provider must not hold the group's decision slot
                # forever; TimeoutError below maps to the llm_failed backoff.
                return await asyncio.wait_for(coro, timeout)
            return await coro

        try:
            response = await _generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        except TypeError:
            # Older signature without ``system_prompt``.
            try:
                response = await _generate(
                    chat_provider_id=provider_id,
                    prompt=f"{system_prompt}\n\n{prompt}",
                )
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] llm_generate failed: {exc}")
                return None
        except asyncio.TimeoutError:
            logger.warning(f"[{PLUGIN_NAME}] llm_generate timed out after {timeout}s for {umo}")
            return None
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] llm_generate failed: {exc}")
            return None
        return getattr(response, "completion_text", "") or ""

    async def _send_message(self, umo: str, text: str, mention_user_id: Optional[str] = None) -> bool:
        try:
            chain = self._build_chain(text, mention_user_id)
            coro = self.context.send_message(umo, chain)
            # AstrBot's contract: True on success, False when no platform
            # handles the session, exceptions on transport errors.
            result = (
                await asyncio.wait_for(coro, SEND_TIMEOUT_SECONDS)
                if SEND_TIMEOUT_SECONDS > 0
                else await coro
            )
            return bool(result)
        except asyncio.TimeoutError:
            logger.error(
                f"[{PLUGIN_NAME}] send_message timed out after {SEND_TIMEOUT_SECONDS}s for {umo}"
            )
            return False
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] send_message failed: {exc}")
            return False

    def _build_chain(self, text: str, mention_user_id: Optional[str]):
        """Build a MessageChain, optionally @-mentioning a user.

        Falls back to plain text when the At component is unavailable; the
        plugin never fails a reply just because it could not mention.
        """
        if mention_user_id:
            try:
                from astrbot.api.message_components import At, Plain

                return MessageChain([At(qq=mention_user_id), Plain(text=text)])
            except Exception as exc:  # pragma: no cover - depends on AstrBot version
                logger.debug(f"[{PLUGIN_NAME}] mention fallback to plain text: {exc}")
        return MessageChain().message(text)

    async def _memory_writeback(self, umo: str, user_text: str, assistant_text: str) -> None:
        try:
            from astrbot.core.agent.message import (
                AssistantMessageSegment,
                TextPart,
                UserMessageSegment,
            )
        except Exception:
            return
        try:
            conversation_manager = self.context.conversation_manager
            conversation_id = await conversation_manager.get_curr_conversation_id(umo)
            if not conversation_id:
                return
            await conversation_manager.add_message_pair(
                cid=conversation_id,
                user_message=UserMessageSegment(
                    content=[TextPart(text=build_writeback_user_text(user_text))]
                ),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=assistant_text)]
                ),
            )
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] memory writeback skipped: {exc}")

    # -- event handlers --------------------------------------------------

    @staticmethod
    def _extract_relations(event: AstrMessageEvent) -> tuple[str, list[str]]:
        """Best-effort extraction of quoted message id and @-ed users.

        Uses the message component class names so it keeps working across
        AstrBot adapters/versions; anything unrecognized is ignored.
        """
        reply_to = ""
        at_users: list[str] = []
        message_obj = getattr(event, "message_obj", None)
        parts = getattr(message_obj, "message", None)
        if not isinstance(parts, (list, tuple)):
            return reply_to, at_users
        for part in parts:
            name = type(part).__name__.lower()
            if name == "reply":
                identifier = getattr(part, "id", None) or getattr(part, "message_id", None)
                reply_to = str(identifier or "")
            elif name == "at":
                qq = getattr(part, "qq", None)
                if qq is None:
                    continue
                token = str(qq)
                if token and token.lower() != "all":
                    at_users.append(token)
        return reply_to, at_users

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """Observe group messages. Never stops propagation, never replies."""
        try:
            message_obj = getattr(event, "message_obj", None)
            sender_id = str(event.get_sender_id())
            self_id = getattr(message_obj, "self_id", None)
            is_bot = bool(self_id) and str(self_id) == sender_id
            message_id = str(getattr(message_obj, "message_id", "") or "")
            reply_to, at_users = ("", [])
            if self.cfg.extract_message_segments:
                reply_to, at_users = self._extract_relations(event)
            kind, text = normalize_content(
                event.message_str or "", self._component_names(event)
            )
            await self.engine.handle_message(
                event.unified_msg_origin,
                message_id=message_id,
                sender=sender_id,
                text=text,
                is_bot=is_bot,
                kind=kind,
                mentioned=bool(getattr(event, "is_at_or_wake_command", False)),
                reply_to=reply_to,
                at_users=at_users,
            )
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] on_group_message failed: {exc}")

    @staticmethod
    def _component_names(event: AstrMessageEvent) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        parts = getattr(message_obj, "message", None)
        if not isinstance(parts, (list, tuple)):
            return []
        return [type(part).__name__ for part in parts]

    @filter.on_llm_request()
    async def inject_group_context(self, event: AstrMessageEvent, req: Any) -> None:
        """Add a temporary group-context block to normal LLM requests."""
        try:
            if not self.cfg.inject_group_context:
                return
            umo = event.unified_msg_origin
            if not self.engine.is_allowed_group(umo):
                return
            state = self.engine.states.get(umo)
            if state is None:
                return
            hint = build_group_hint(state, self.cfg, time.time())
            if not hint:
                return
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=hint)
            mark = getattr(part, "mark_as_temp", None)
            if callable(mark):
                part = mark()
            req.extra_user_content_parts.append(part)
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] inject_group_context skipped: {exc}")

    # -- admin commands --------------------------------------------------

    @filter.command_group("ws")
    def ws(self) -> None:  # pragma: no cover - declarative
        """鲸鱼娘社交引擎管理指令。"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ws.command("status")
    async def ws_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        state = self.engine.states.get(umo)
        lines = [
            f"插件：{'开启' if self.cfg.enabled else '关闭'}｜本群："
            f"{'已启用' if self.engine.is_allowed_group(umo) else '未启用'}",
            f"演练模式：{'是' if self.cfg.dry_run else '否'}",
        ]
        if state is not None:
            now = time.time()
            remaining = max(0, int(state.next_speak_after - now))
            lines.append(f"冷却剩余：{remaining}s")
            if state.send_blocked_until > now:
                lines.append(f"失败退避剩余：{int(state.send_blocked_until - now)}s")
            lines.append(f"社交能量：{state.social_energy:.2f}")
            lines.append(f"今日主动：{state.proactive_sent_today}/{self.cfg.daily_proactive_cap}")
            lines.append(f"连续机器人发言：{state.consecutive_bot_messages}")
            if state.llm_failure_count:
                lines.append(f"决策模型失败累计：{state.llm_failure_count}")
            active_threads = [thread for thread in state.threads if not thread.ended]
            lines.append(f"活跃会话：{len(active_threads)}")
            if state.debounce_deadline > now:
                lines.append(f"防抖等待：{max(0.0, state.debounce_deadline - now):.1f}s")
            lines.append(f"上次决策：{self.engine.last_decision.get(umo, '无')}")
        global_state = self.engine.flow.global_state
        lines.append(
            f"全局今日主动：{global_state.proactive_sent_today}/{self.cfg.global_daily_proactive_cap}"
        )
        lines.append(f"任务阶段：{self.engine.phase(umo)}")
        dedup = self.engine.deduplicator.stats()
        lines.append(f"去重缓存：{dedup['size']} 条（累计丢弃重复 {dedup['duplicates']} 条）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ws.command("enable")
    async def ws_enable(self, event: AstrMessageEvent):
        if self.engine.enable_group(event.unified_msg_origin):
            yield event.plain_result("已启用本群的主动社交（仅本次运行有效；持久请改 WebUI 配置）。")
        else:
            yield event.plain_result("本群已在启用列表中。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ws.command("disable")
    async def ws_disable(self, event: AstrMessageEvent):
        if self.engine.disable_group(event.unified_msg_origin):
            yield event.plain_result("已停用本群的主动社交（持久请改 WebUI 配置）。")
        else:
            yield event.plain_result("本群本来就没有启用。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ws.command("reset")
    async def ws_reset(self, event: AstrMessageEvent):
        self.engine.reset_group(event.unified_msg_origin)
        yield event.plain_result("已重置本群的社交状态。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ws.command("why")
    async def ws_why(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        state = self.engine.states.get(umo)
        decision = self.engine.last_decision.get(umo, "无")
        if state is None:
            yield event.plain_result(f"本群暂无状态。上次决策：{decision}")
            return
        moment = local_datetime(time.time(), self.cfg.timezone)
        backoff = ""
        if state.send_blocked_until > time.time():
            backoff = f"失败退避剩余：{int(state.send_blocked_until - time.time())}s\n"
        active_threads = [thread for thread in state.threads if not thread.ended]
        thread_lines = "\n".join(
            f"  - {thread.id}｜{thread.topic or '未识别'}｜{len(thread.messages)}条｜"
            f"活跃度 {thread.activity_score:.2f}"
            for thread in active_threads
        )
        if thread_lines:
            thread_lines = f"\n活跃会话：\n{thread_lines}"
        yield event.plain_result(
            f"上次决策：{decision}\n"
            f"任务阶段：{self.engine.phase(umo)}\n"
            f"冷却：{'进行中' if state.next_speak_after > time.time() else '已就绪'}\n"
            f"{backoff}"
            f"连续机器人发言：{state.consecutive_bot_messages}\n"
            f"被忽略：{'是' if state.last_bot_ignored else '否'}\n"
            f"{thread_lines}\n"
            f"当前时间：{moment.strftime('%H:%M:%S')}（允许时段 {self.cfg.active_hours}）"
        )
