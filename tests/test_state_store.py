from core.models import GroupState
from storage.state_store import StateStore


def test_roundtrip_persists_counters(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = GroupState(
        social_energy=0.4,
        next_speak_after=123.0,
        consecutive_bot_messages=2,
        daily_reset_date="2026-01-01",
        last_bot_ignored=True,
    )
    store.save({"umo": state.to_persist_dict()})

    loaded = store.load()
    assert loaded["umo"]["next_speak_after"] == 123.0
    assert loaded["umo"]["consecutive_bot_messages"] == 2
    assert loaded["umo"]["last_bot_ignored"] is True
    assert loaded["umo"]["daily_reset_date"] == "2026-01-01"


def test_missing_file_returns_empty(tmp_path):
    assert StateStore(tmp_path / "nope.json").load() == {}


def test_schema_mismatch_is_discarded(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": 999, "states": {"a": {}}}', encoding="utf-8")
    assert StateStore(path).load() == {}


def test_corrupt_json_returns_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert StateStore(path).load() == {}


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save({"a": {"social_energy": 1.0}})
    assert (tmp_path / "state.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_overwrites_previous_payload(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save({"a": {"social_energy": 0.1}})
    store.save({"b": {"social_energy": 0.9}})
    loaded = store.load()
    assert "a" not in loaded
    assert loaded["b"]["social_energy"] == 0.9


def test_from_persist_dict_ignores_unknown_and_malformed_fields():
    state = GroupState.from_persist_dict(
        {
            "social_energy": "0.7",
            "consecutive_bot_messages": "abc",
            "unknown_field": 1,
            "last_proactive_msg": None,
        }
    )
    assert state.social_energy == 0.7
    assert state.consecutive_bot_messages == 0  # malformed int keeps default
    assert state.last_proactive_msg == ""


def test_global_payload_roundtrip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(
        {"a": {"social_energy": 1.0}},
        {"proactive_sent_today": 3, "daily_reset_date": "2026-01-01"},
    )
    assert store.load()["a"]["social_energy"] == 1.0
    global_state = store.load_global()
    assert global_state["proactive_sent_today"] == 3
    assert global_state["daily_reset_date"] == "2026-01-01"


def test_global_payload_absent_returns_empty(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save({"a": {"social_energy": 1.0}})
    assert store.load_global() == {}
