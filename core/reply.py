"""Outgoing reply sanitising (no AstrBot imports)."""

from __future__ import annotations

from typing import Iterable, Optional

_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",  # curly double quotes
    "\u2018": "\u2019",  # curly single quotes
    "\u300c": "\u300d",  # CJK corner brackets
}


def _strip_wrapping_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] in _QUOTE_PAIRS and text[-1] == _QUOTE_PAIRS[text[0]]:
        return text[1:-1].strip()
    return text


def sanitize_reply(
    text: Optional[str],
    blocklist: Iterable[str],
    *,
    max_length: int = 200,
) -> Optional[str]:
    """Return a clean single-line reply, or ``None`` when unusable.

    A ``None`` result means "do not send": empty content, a blocked term, or
    nothing left after trimming.
    """
    if not text:
        return None
    value = _strip_wrapping_quotes(str(text).strip())
    value = " ".join(value.split())
    if not value:
        return None

    lowered = value.lower()
    for bad in blocklist:
        needle = (bad or "").strip().lower()
        if needle and needle in lowered:
            return None

    if max_length > 0 and len(value) > max_length:
        value = value[:max_length].rstrip()
    return value or None
