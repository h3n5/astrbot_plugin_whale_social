from core.reply import sanitize_reply


def test_trims_and_collapses_whitespace():
    assert sanitize_reply("  你好\n  世界  ", []) == "你好 世界"


def test_strips_wrapping_quotes():
    assert sanitize_reply('"你好"', []) == "你好"
    assert sanitize_reply("\u201c你好\u201d", []) == "你好"


def test_blocklist_drops_reply():
    assert sanitize_reply("这就是广告", ["广告"]) is None


def test_blank_returns_none():
    assert sanitize_reply("   ", []) is None
    assert sanitize_reply(None, []) is None


def test_truncates_to_max_length():
    assert sanitize_reply("a" * 300, [], max_length=10) == "a" * 10


def test_blocklist_is_case_insensitive():
    assert sanitize_reply("Buy VIP now", ["vip"]) is None
