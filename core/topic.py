"""Topic / interest analysis (V1: keyword based)."""

from __future__ import annotations

from typing import Iterable, Optional


def normalize(text: Optional[str]) -> str:
    return " ".join((text or "").lower().split())


def keyword_hits(text: Optional[str], keywords: Iterable[str]) -> list[str]:
    haystack = normalize(text)
    if not haystack:
        return []
    hits: list[str] = []
    for keyword in keywords:
        needle = normalize(keyword)
        if needle and needle in haystack:
            hits.append(keyword)
    return hits


def negative_hit(text: Optional[str], negatives: Iterable[str]) -> Optional[str]:
    haystack = normalize(text)
    if not haystack:
        return None
    for keyword in negatives:
        needle = normalize(keyword)
        if needle and needle in haystack:
            return keyword
    return None


def interest_multiplier(
    text: Optional[str],
    keywords: Iterable[str],
    negatives: Iterable[str],
    bonus: float,
) -> tuple[float, list[str], Optional[str]]:
    """Return ``(multiplier, matched_keywords, negative_keyword)``.

    A negative hit yields a multiplier of ``0.0``.
    """
    blocked = negative_hit(text, negatives)
    if blocked is not None:
        return 0.0, [], blocked
    hits = keyword_hits(text, keywords)
    if hits:
        return min(1.0 + 0.7 * len(hits), bonus), hits, None
    # Baseline for keyword-less chatter. It is deliberately mild: groups whose
    # topics fall outside the keyword list must still reach the decision LLM
    # sometimes, otherwise the probability funnel closes entirely.
    return 0.6, [], None


def topic_still_relevant(recent_texts: Iterable[str], keywords: Iterable[str]) -> bool:
    return bool(keyword_hits(" ".join(recent_texts), keywords))
