"""Signal gate: decides whether a message is worth considering at all."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from core.config import parse_active_hours

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.config import PluginConfig
    from core.flow import FlowController
    from core.models import GroupState


@dataclass
class GateResult:
    allowed: bool
    reason: str


def rate_in_window(state: "GroupState", now: float, seconds: float) -> int:
    return sum(1 for stamp in state.message_times if now - stamp <= seconds)


def within_active_hours(active_hours: str, moment: datetime) -> bool:
    start, end = parse_active_hours(active_hours)
    if start == end:
        return True
    current = moment.hour * 60 + moment.minute
    if start < end:
        return start <= current < end
    # Window crosses midnight, e.g. 22:00-06:00.
    return current >= start or current < end


def check_gate(
    state: "GroupState",
    config: "PluginConfig",
    now: float,
    moment: datetime,
    *,
    mentioned: bool = False,
    flow: "FlowController | None" = None,
) -> GateResult:
    """Evaluate the local gate. Order matters; the first failure wins."""
    if not config.enabled:
        return GateResult(False, "disabled")
    if mentioned:
        return GateResult(True, "mentioned")
    if state.next_speak_after > 0 and now < state.next_speak_after:
        return GateResult(False, "cooldown")
    if rate_in_window(state, now, 30) > config.incoming_rate_limit:
        return GateResult(False, "rate_limit")
    if state.consecutive_bot_messages >= 1:
        return GateResult(False, "consecutive_bot")
    if flow is not None:
        allowed, reason = flow.check(state, now)
        if not allowed:
            return GateResult(False, reason)
    if not within_active_hours(config.active_hours, moment):
        return GateResult(False, "quiet_hours")
    if config.daily_proactive_cap > 0 and state.proactive_sent_today >= config.daily_proactive_cap:
        return GateResult(False, "daily_cap")
    return GateResult(True, "ok")
