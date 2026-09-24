"""Tests for reconnect-replay de-duplication (message layer + envelope)."""

from core.dedup import MessageDeduplicator
from core.models import MessageEnvelope


def test_first_seen_then_duplicate():
    dedup = MessageDeduplicator(ttl_seconds=300.0)
    assert dedup.check_and_add("k", 1000.0) is False
    assert dedup.check_and_add("k", 1000.0) is True
    assert dedup.accepted == 1
    assert dedup.duplicates == 1
    assert len(dedup) == 1


def test_empty_key_is_never_a_duplicate():
    dedup = MessageDeduplicator()
    assert dedup.check_and_add("", 1.0) is False
    assert dedup.check_and_add("", 1.0) is False
    assert len(dedup) == 0


def test_key_expires_after_ttl():
    dedup = MessageDeduplicator(ttl_seconds=300.0)
    dedup.check_and_add("k", 1000.0)
    assert dedup.check_and_add("k", 1299.0) is True  # still within TTL
    # now - ttl = 1001 > 1000, so the original entry is purged and re-added.
    assert dedup.check_and_add("k", 1301.0) is False


def test_max_entries_evicts_oldest():
    dedup = MessageDeduplicator(ttl_seconds=0.0, max_entries=2)
    dedup.check_and_add("a", 1.0)
    dedup.check_and_add("b", 2.0)
    dedup.check_and_add("c", 3.0)
    assert len(dedup) == 2
    assert dedup.check_and_add("a", 4.0) is False  # "a" had been evicted
    assert dedup.check_and_add("c", 5.0) is True  # "c" still present


def test_zero_max_entries_is_unbounded():
    dedup = MessageDeduplicator(ttl_seconds=0.0, max_entries=0)
    for index in range(50):
        dedup.check_and_add(f"k{index}", float(index))
    assert len(dedup) == 50


def test_is_duplicate_is_read_only():
    dedup = MessageDeduplicator(ttl_seconds=300.0)
    assert dedup.is_duplicate("k", 1.0) is False
    dedup.check_and_add("k", 1.0)
    assert dedup.is_duplicate("k", 1.0) is True
    assert dedup.duplicates == 0  # read path does not count


def test_reset_clears_everything():
    dedup = MessageDeduplicator()
    dedup.check_and_add("k", 1.0)
    dedup.reset()
    assert len(dedup) == 0
    assert dedup.check_and_add("k", 1.0) is False


def test_envelope_dedup_key_requires_message_id():
    envelope = MessageEnvelope(umo="q:g:1", message_id="9", sender="u", text="hi", timestamp=15.0)
    assert envelope.dedup_key() == "q:g:1:9"
    blank = MessageEnvelope(umo="q:g:1", message_id="  ", sender="u", text="hi", timestamp=15.0)
    assert blank.dedup_key() == ""


def test_envelope_fingerprint_buckets():
    base = MessageEnvelope(umo="q:g:1", message_id="", sender="u", text="哈哈", timestamp=15.0)
    same_bucket = MessageEnvelope(umo="q:g:1", message_id="", sender="u", text="哈哈", timestamp=11.0)
    next_bucket = MessageEnvelope(umo="q:g:1", message_id="", sender="u", text="哈哈", timestamp=25.0)
    assert base.fingerprint(10) == same_bucket.fingerprint(10)
    assert base.fingerprint(10) != next_bucket.fingerprint(10)
    assert base.fingerprint(0) == ""


def test_envelope_fingerprint_distinguishes_text_and_sender():
    first = MessageEnvelope(umo="q:g:1", message_id="", sender="u1", text="哈哈", timestamp=15.0)
    other_text = MessageEnvelope(umo="q:g:1", message_id="", sender="u1", text="呵呵", timestamp=15.0)
    other_sender = MessageEnvelope(umo="q:g:1", message_id="", sender="u2", text="哈哈", timestamp=15.0)
    assert first.fingerprint(10) != other_text.fingerprint(10)
    assert first.fingerprint(10) != other_sender.fingerprint(10)
