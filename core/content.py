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
    "share": "分享",
    "json": "卡片",
    "xml": "卡片",
    "node": "转发消息",
    "nodes": "转发消息",
    "poke": "戳一戳",
}

# Cap on one message's text inside transcripts / decision prompts. The
# decision LLM call is text-only (placeholders instead of media), and a
# text-only model must never receive a wall of text — pasted articles, very
# long URLs or potential base64 payloads — via ``message_str``.
TEXT_MAX_CHARS = 500
GENERIC_PLACEHOLDER = "[消息]"


def _cap_text(text: str) -> str:
    if len(text) > TEXT_MAX_CHARS:
        return text[:TEXT_MAX_CHARS] + "…"
    return text


def normalize_content(text: str, part_names: Iterable[str]) -> tuple[str, str]:
    """Return ``(kind, text)`` for an observed message.

    Plain text wins (capped at ``TEXT_MAX_CHARS``). When the platform rendered
    no text, the first recognized non-text component becomes a placeholder
    (e.g. ``("image", "[图片]")``); truly unknown components fall back to a
    generic ``[消息]`` placeholder so the message is not silently dropped as
    empty. Media itself never reaches the LLM — the decision model only ever
    sees these text placeholders.
    """
    if (text or "").strip():
        return "text", _cap_text(text)
    names = [str(name).lower() for name in part_names]
    for key in names:
        label = KIND_LABELS.get(key)
        if label:
            return key, f"[{label}]"
    if names:
        return names[0] or "text", GENERIC_PLACEHOLDER
    return "text", text
