"""Timezone helpers with graceful fallback (no AstrBot imports)."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Optional

try:  # pragma: no cover - availability depends on the host
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]


def resolve_timezone(name: Optional[str]) -> Optional[tzinfo]:
    """Return a tzinfo for an IANA name, or ``None`` to fall back to local."""
    if not name:
        return None
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(str(name))
    except Exception:
        # Unknown name or missing tzdata; caller falls back to local time.
        return None


def local_datetime(now: float, tz_name: Optional[str]) -> datetime:
    tz = resolve_timezone(tz_name)
    if tz is None:
        return datetime.fromtimestamp(now)
    return datetime.fromtimestamp(now, tz)


def day_key(now: float, tz_name: Optional[str]) -> str:
    """Day bucket (YYYY-MM-DD) used for daily quotas."""
    return local_datetime(now, tz_name).strftime("%Y-%m-%d")
