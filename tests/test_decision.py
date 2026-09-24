from core.config import PluginConfig
from core.decision import (
    build_decision_prompt,
    build_system_prompt,
    extract_json_object,
    parse_decision,
)


def test_parses_plain_json():
    decision = parse_decision(
        '{"action":"SPEAK","reason":"interesting","topic":"副本","reply":"带我一个"}'
    )
    assert decision is not None
    assert decision.action == "SPEAK"
    assert decision.reply == "带我一个"
    assert decision.topic == "副本"


def test_parses_fenced_json():
    raw = "```json\n{\"action\": \"ignore\", \"reason\": \"no\"}\n```"
    decision = parse_decision(raw)
    assert decision is not None
    assert decision.action == "IGNORE"


def test_tolerates_surrounding_prose():
    raw = "我的判断是：{\"action\":\"WAIT\",\"topic\":\"抽卡\"} 就这样。"
    decision = parse_decision(raw)
    assert decision is not None
    assert decision.action == "WAIT"
    assert decision.topic == "抽卡"


def test_handles_braces_inside_strings():
    raw = '{"action":"SPEAK","reply":"用 {大括号} 也行"}'
    decision = parse_decision(raw)
    assert decision is not None
    assert decision.reply == "用 {大括号} 也行"


def test_rejects_unknown_action():
    assert parse_decision('{"action":"DANCE"}') is None


def test_rejects_non_json():
    assert parse_decision("我觉得可以聊两句") is None
    assert parse_decision("") is None


def test_extract_returns_none_for_unbalanced():
    assert extract_json_object('{"action": "SPEAK"') is None


def test_system_prompt_contains_json_contract():
    config = PluginConfig(decision_prompt="")
    prompt = build_system_prompt(config)
    assert "JSON" in prompt
    assert "action" in prompt


def test_custom_decision_prompt_overrides_default():
    config = PluginConfig(decision_prompt="只输出 JSON")
    assert build_system_prompt(config).startswith("只输出 JSON")


def test_decision_prompt_includes_context_and_hint():
    prompt = build_decision_prompt(
        context_text="[00:00:00] alice: hi",
        trigger_text="hi",
        hint="<dynamic_context>x</dynamic_context>",
    )
    assert "alice" in prompt
    assert "hi" in prompt
    assert "dynamic_context" in prompt
