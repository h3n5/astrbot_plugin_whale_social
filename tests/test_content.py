"""Tests for non-text content normalization at the entry point."""

from core.content import normalize_content


def test_plain_text_is_unchanged():
    assert normalize_content("今晚打副本", ["Plain"]) == ("text", "今晚打副本")


def test_empty_text_with_image_component_gets_placeholder():
    assert normalize_content("", ["Image", "Plain"]) == ("image", "[图片]")


def test_whitespace_only_text_with_record_component():
    assert normalize_content("   ", ["Record"]) == ("record", "[语音]")


def test_unknown_components_fall_back_to_empty_text():
    assert normalize_content("", ["Node", "Poke"]) == ("text", "")


def test_first_recognized_component_wins():
    assert normalize_content("", ["Reply", "Image", "Video"]) == ("image", "[图片]")
