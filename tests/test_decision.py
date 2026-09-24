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


def test_parses_v2_thread_and_target():
    decision = parse_decision(
        '{"action":"SPEAK","thread_id":"t2","target":{"type":"user","user_id":"42"},'
        '"reply":"来"}'
    )
    assert decision is not None
    assert decision.thread_id == "t2"
    assert decision.target_type == "USER"
    assert decision.target_user_id == "42"


def test_group_target_drops_user_id():
    decision = parse_decision(
        '{"action":"SPEAK","target":{"type":"GROUP","user_id":"42"},"reply":"x"}'
    )
    assert decision is not None
    assert decision.target_type == "GROUP"
    assert decision.target_user_id == ""


def test_invalid_target_type_downgrades_to_group():
    decision = parse_decision('{"action":"SPEAK","target":{"type":"CHANNEL"},"reply":"x"}')
    assert decision is not None
    assert decision.target_type == "GROUP"


def test_missing_v2_fields_default():
    decision = parse_decision('{"action":"IGNORE"}')
    assert decision is not None
    assert decision.thread_id == ""
    assert decision.target_type == "GROUP"
    assert decision.target_user_id == ""


def test_decision_prompt_includes_thread_info():
    prompt = build_decision_prompt(
        context_text="ctx",
        trigger_text="hi",
        thread_id="t1",
        threads_overview="- t2｜话题：抽卡",
    )
    assert "t1" in prompt
    assert "t2" in prompt



def test_system_prompt_is_persona_agnostic():
    from core.config import DEFAULT_DECISION_PROMPT, PluginConfig
    from core.decision import build_system_prompt

    default_prompt = build_system_prompt(PluginConfig())
    assert default_prompt == DEFAULT_DECISION_PROMPT
    assert "人格" not in default_prompt

    custom = build_system_prompt(PluginConfig(decision_prompt="自定义决策提示"))
    assert custom == "自定义决策提示"
