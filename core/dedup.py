"""Message-level de-duplication (pure Python, time-injectable).

A QQ/OneBot WebSocket reconnect can replay recent events, so the same logical
message may be delivered more than once. The plugin must let it enter the
collector / thread / LLM pipeline only once.

This is the *first* gate in the pipeline: it runs before ``GroupState`` is
created and before anything touches the LLM, so a duplicate is invisible to
every downstream layer.

Keys are ``umo:message_id`` (the UMO already encodes platform + group), never
``group+user+text`` — a user may genuinely send the same text twice.

We find the duplicate in ``O(1)`` with an ``OrderedDict`` used as a TTL + bounded
cache. Entries are kept in first-seen order (which tracks the non-decreasing
clock), so expiry can stop at the first live entry; when the cache is full the
oldest entry is evicted.
"""

from __future__ import annotations

from collections import OrderedDict

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 10000


class MessageDeduplicator:
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries) if max_entries and max_entries > 0 else 0
        self._entries: "OrderedDict[str, float]" = OrderedDict()
        self.accepted = 0
        self.duplicates = 0

    def __len__(self) -> int:
        return len(self._entries)

    def _purge(self, now: float) -> None:
        ttl = self.ttl_seconds
        if ttl <= 0:
            return
        cutoff = now - ttl
        # Insertion order is roughly timestamp order, so stop at the first live
        # entry instead of scanning the whole cache.
        for key in list(self._entries.keys()):
            if self._entries[key] < cutoff:
                del self._entries[key]
            else:
                break

    def check_and_add(self, key: str, now: float) -> bool:
        """Return ``True`` if ``key`` was seen recently, else record it.

        An empty key is never considered a duplicate (an event without a stable
        id must not collide with every other id-less event).
        """
        if not key:
            return False
        self._purge(now)
        if key in self._entries:
            self.duplicates += 1
            return True
        self._entries[key] = now
        self.accepted += 1
        if self.max_entries and len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return False

    def is_duplicate(self, key: str, now: float) -> bool:
        """Read-only variant that does not add the key."""
        if not key:
            return False
        self._purge(now)
        return key in self._entries

    def reset(self) -> None:
        self._entries.clear()
        self.accepted = 0
        self.duplicates = 0

    def stats(self) -> dict[str, int | float]:
        return {
            "size": len(self._entries),
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "ttl_seconds": self.ttl_seconds,
        }
