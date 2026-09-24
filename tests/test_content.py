"""Tests for non-text content normalization at the entry point."""

from core.content import normalize_content


def test_plain_text_is_unchanged():
    assert normalize_content("今晚打副本", ["Plain"]) == ("text", "今晚打副本")


def test_empty_text_with_image_component_gets_placeholder():
    assert normalize_content("", ["Image", "Plain"]) == ("image", "[图片]")


def test_whitespace_only_text_with_record_component():
    assert normalize_content("   ", ["Record"]) == ("record", "[语音]")


def test_first_recognized_component_wins():
    assert normalize_content("", ["Reply", "Image", "Video"]) == ("image", "[图片]")


def test_forward_and_poke_components_get_placeholders():
    assert normalize_content("", ["Node", "Poke"]) == ("node", "[转发消息]")


def test_share_and_card_components_get_placeholders():
    assert normalize_content("", ["Share"]) == ("share", "[分享]")
    assert normalize_content("", ["Json"]) == ("json", "[卡片]")


def test_truly_unknown_component_gets_generic_placeholder():
    assert normalize_content("", ["MysteryComponent"]) == ("mysterycomponent", "[消息]")


def test_long_text_is_capped():
    kind, text = normalize_content("x" * 600, [])
    assert kind == "text"
    assert text == "x" * 500 + "…"
