"""Per-group debounce deadline policy (pure; scheduling lives in the engine).

A burst of messages keeps pushing the deadline back, but never beyond
``debounce_max_wait_seconds`` past the first trigger, so a high-traffic group
still gets a decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import PluginConfig
    from .models import GroupState


class DebounceTracker:
    @staticmethod
    def arm(state: "GroupState", config: "PluginConfig", now: float) -> float:
        """(Re)arm the deadline for a group and return it."""
        if state.debounce_deadline <= 0:
            state.debounce_first_trigger = now
        quiet = max(0.0, float(config.debounce_seconds))
        deadline = now + quiet
        max_wait = float(config.debounce_max_wait_seconds)
        if max_wait > 0:
            deadline = min(deadline, state.debounce_first_trigger + max_wait)
        state.debounce_deadline = deadline
        return deadline

    @staticmethod
    def remaining(state: "GroupState", now: float) -> float:
        if state.debounce_deadline <= 0:
            return 0.0
        return state.debounce_deadline - now

    @staticmethod
    def due(state: "GroupState", now: float) -> bool:
        return state.debounce_deadline > 0 and now >= state.debounce_deadline

    @staticmethod
    def clear(state: "GroupState") -> None:
        state.debounce_deadline = 0.0
        state.debounce_first_trigger = 0.0
