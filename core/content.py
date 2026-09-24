"""Normalization of non-text message payloads (AstrBot-independent).

AstrBot adapters expose different component class names per platform. The
entry point only needs a coarse ``kind`` plus a short placeholder so non-text
events keep their semantics in threads, scoring and the decision prompt.
"""

from __future__ import annotations

from typing import Iterable

# Component class name (lowercased) -> placeholder label.
KIND_LABELS: dict[str, str] = {
    "image": "图片",
    "record": "语音",
    "video": "视频",
    "file": "文件",
    "face": "表情",
}


def normalize_content(text: str, part_names: Iterable[str]) -> tuple[str, str]:
    """Return ``(kind, text)`` for an observed message.

    Plain text wins. When the platform rendered no text, the first recognized
    non-text component becomes a placeholder (e.g. ``("image", "[图片]")``) so
    the message is not silently dropped as empty.
    """
    if (text or "").strip():
        return "text", text
    for name in part_names:
        key = str(name).lower()
        label = KIND_LABELS.get(key)
        if label:
            return key, f"[{label}]"
    return "text", text
