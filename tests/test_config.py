"""Tests for config parsing of the newer options."""

from core.config import PluginConfig


def test_new_fields_default():
    config = PluginConfig()
    assert config.llm_timeout_seconds == 60
    assert config.max_reply_length == 200


def test_new_fields_parse_from_mapping():
    config = PluginConfig.from_mapping(
        {"llm_timeout_seconds": "30", "max_reply_length": 0}
    )
    assert config.llm_timeout_seconds == 30
    assert config.max_reply_length == 0


def test_new_fields_fall_back_on_garbage():
    config = PluginConfig.from_mapping(
        {"llm_timeout_seconds": "abc", "max_reply_length": None}
    )
    assert config.llm_timeout_seconds == 60
    assert config.max_reply_length == 200


def test_blocklist_and_reply_length_roundtrip():
    config = PluginConfig.from_mapping(
        {"output_blocklist": "广告\n代练", "max_reply_length": 50}
    )
    assert config.blocklist() == ["广告", "代练"]
    assert config.max_reply_length == 50
