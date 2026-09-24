"""Flow control: token buckets, daily quotas and send-failure backoff.

Per-group bucket/backoff state lives on :class:`core.models.GroupState` so it
survives restarts; the cross-group bucket and global daily counter live on a
small :class:`GlobalFlowState` persisted alongside.

No AstrBot imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional

from .timeutil import day_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import PluginConfig
    from .models import GroupState


@dataclass
class GlobalFlowState:
    bucket_tokens: float = 0.0
    bucket_updated_at: float = 0.0
    proactive_sent_today: int = 0
    daily_reset_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket_tokens": self.bucket_tokens,
            "bucket_updated_at": self.bucket_updated_at,
            "proactive_sent_today": self.proactive_sent_today,
            "daily_reset_date": self.daily_reset_date,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GlobalFlowState":
        def _float(value: Any, default: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def _int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        if not isinstance(data, Mapping):
            return cls()
        return cls(
            bucket_tokens=_float(data.get("bucket_tokens", 0.0), 0.0),
            bucket_updated_at=_float(data.get("bucket_updated_at", 0.0), 0.0),
            proactive_sent_today=_int(data.get("proactive_sent_today", 0), 0),
            daily_reset_date=str(data.get("daily_reset_date", "") or ""),
        )


def refill_tokens(
    current: float,
    updated_at: float,
    capacity: float,
    refill_seconds: float,
    now: float,
) -> float:
    """Lazily refill a token bucket (continuous, 1 token per refill_seconds)."""
    if capacity <= 0:
        return 0.0
    if updated_at <= 0 or refill_seconds <= 0:
        return float(capacity)
    elapsed = max(0.0, now - updated_at)
    return min(float(capacity), float(current) + elapsed / float(refill_seconds))


class FlowController:
    def __init__(
        self,
        config: "PluginConfig",
        global_state: Optional[GlobalFlowState] = None,
    ) -> None:
        self.config = config
        self.global_state = global_state or GlobalFlowState()

    # -- daily reset -----------------------------------------------------

    def _ensure_global_day(self, now: float) -> None:
        today = day_key(now, self.config.timezone)
        if self.global_state.daily_reset_date != today:
            self.global_state.daily_reset_date = today
            self.global_state.proactive_sent_today = 0

    # -- checks ----------------------------------------------------------

    def check(self, state: "GroupState", now: float) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` without consuming anything."""
        if state.send_blocked_until > 0 and now < state.send_blocked_until:
            return False, "failure_backoff"
        self._ensure_global_day(now)

        config = self.config
        if config.group_token_bucket_capacity > 0:
            tokens = refill_tokens(
                state.group_tokens,
                state.group_tokens_updated_at,
                config.group_token_bucket_capacity,
                config.group_token_refill_seconds,
                now,
            )
            if tokens < 1.0:
                return False, "group_bucket"

        global_bucket = self.global_state
        if config.global_token_bucket_capacity > 0:
            tokens = refill_tokens(
                global_bucket.bucket_tokens,
                global_bucket.bucket_updated_at,
                config.global_token_bucket_capacity,
                config.global_token_refill_seconds,
                now,
            )
            if tokens < 1.0:
                return False, "global_bucket"

        if (
            config.global_daily_proactive_cap > 0
            and global_bucket.proactive_sent_today >= config.global_daily_proactive_cap
        ):
            return False, "global_daily_cap"
        return True, "ok"

    # -- reservation -----------------------------------------------------

    def reserve(self, state: "GroupState", now: float) -> None:
        """Consume one group + global token. Must be paired with commit/rollback."""
        config = self.config
        if config.group_token_bucket_capacity > 0:
            tokens = refill_tokens(
                state.group_tokens,
                state.group_tokens_updated_at,
                config.group_token_bucket_capacity,
                config.group_token_refill_seconds,
                now,
            )
            state.group_tokens = max(0.0, tokens - 1.0)
            state.group_tokens_updated_at = now

        global_bucket = self.global_state
        if config.global_token_bucket_capacity > 0:
            tokens = refill_tokens(
                global_bucket.bucket_tokens,
                global_bucket.bucket_updated_at,
                config.global_token_bucket_capacity,
                config.global_token_refill_seconds,
                now,
            )
            global_bucket.bucket_tokens = max(0.0, tokens - 1.0)
            global_bucket.bucket_updated_at = now

    def commit_success(self, state: "GroupState", now: float) -> None:
        self._ensure_global_day(now)
        self.global_state.proactive_sent_today += 1
        state.failure_count = 0
        state.send_blocked_until = 0.0

    def rollback(self, state: "GroupState", now: float) -> None:
        """Refund tokens and schedule an exponential backoff after a failure."""
        config = self.config
        if config.group_token_bucket_capacity > 0:
            tokens = refill_tokens(
                state.group_tokens,
                state.group_tokens_updated_at,
                config.group_token_bucket_capacity,
                config.group_token_refill_seconds,
                now,
            )
            state.group_tokens = min(float(config.group_token_bucket_capacity), tokens + 1.0)
            state.group_tokens_updated_at = now

        global_bucket = self.global_state
        if config.global_token_bucket_capacity > 0:
            tokens = refill_tokens(
                global_bucket.bucket_tokens,
                global_bucket.bucket_updated_at,
                config.global_token_bucket_capacity,
                config.global_token_refill_seconds,
                now,
            )
            global_bucket.bucket_tokens = min(float(config.global_token_bucket_capacity), tokens + 1.0)
            global_bucket.bucket_updated_at = now

        state.failure_count += 1
        base = max(0.0, float(config.send_failure_backoff_seconds))
        delay = base * (2 ** (state.failure_count - 1))
        ceiling = float(config.max_failure_backoff_seconds)
        if ceiling > 0:
            delay = min(delay, ceiling)
        state.send_blocked_until = now + delay

    # -- persistence -----------------------------------------------------

    def export_global(self) -> dict[str, Any]:
        return self.global_state.to_dict()

    def load_global(self, data: Optional[Mapping[str, Any]]) -> None:
        if data:
            self.global_state = GlobalFlowState.from_dict(data)
