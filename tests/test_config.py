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


def test_keyword_overrides_accept_list_items():
    config = PluginConfig.from_mapping(
        {"group_keyword_overrides": ["aiocqhttp:GroupMessage:123=游戏,副本", " umo2 = 抽卡，肝 "]}
    )
    assert config.group_keyword_overrides == {
        "aiocqhttp:GroupMessage:123": ["游戏", "副本"],
        "umo2": ["抽卡", "肝"],
    }


def test_keyword_overrides_still_accept_mapping_form():
    config = PluginConfig.from_mapping(
        {"group_keyword_overrides": {"umo1": "游戏\n副本"}}
    )
    assert config.group_keyword_overrides == {"umo1": ["游戏", "副本"]}


def test_keyword_overrides_skip_malformed_items():
    config = PluginConfig.from_mapping(
        {"group_keyword_overrides": ["没有分隔符", "", "=空群号", "umo=ok"]}
    )
    assert config.group_keyword_overrides == {"umo": ["ok"]}
