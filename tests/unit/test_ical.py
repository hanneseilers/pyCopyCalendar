import pytest

from calendar_sync.ical import (
    IcalParseError,
    extract_managed_metadata,
    get_master,
    is_valid_target_uid,
    parse_vevents,
)
from calendar_sync.models import instance_key, target_uid, RecurrenceKey


def _managed_ics(uid, source_id="dept-a", source_uid="src-1", recurrence="SINGLE", fingerprint="fp1"):
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:{uid}
DTSTART:20260101T100000Z
DTEND:20260101T110000Z
SUMMARY:Test
X-CALMIRROR-MANAGED:1
X-CALMIRROR-SOURCE:{source_id}
X-CALMIRROR-SOURCE-UID:{source_uid}
X-CALMIRROR-RECURRENCE-ID:{recurrence}
X-CALMIRROR-FINGERPRINT:{fingerprint}
END:VEVENT
END:VCALENDAR
"""


def test_parse_vevents_empty_raises():
    with pytest.raises(IcalParseError):
        parse_vevents("")


def test_parse_vevents_malformed_raises():
    with pytest.raises(IcalParseError):
        parse_vevents("not even close to icalendar data {{{")


def test_get_master_prefers_component_without_recurrence_id():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-1
RECURRENCE-ID:20260112T090000Z
DTSTART:20260112T090000Z
END:VEVENT
BEGIN:VEVENT
UID:series-1
DTSTART:20260105T090000Z
END:VEVENT
END:VCALENDAR
"""
    vevents = parse_vevents(ics)
    master = get_master(vevents)
    assert "RECURRENCE-ID" not in master


def test_is_valid_target_uid_accepts_own_format():
    key = instance_key("dept-a", "uid1", RecurrenceKey.single())
    assert is_valid_target_uid(target_uid(key))


@pytest.mark.parametrize("bad", ["not-a-uuid@calendar-mirror", "", None, "abc123@wrong-suffix"])
def test_is_valid_target_uid_rejects_bad_shapes(bad):
    assert not is_valid_target_uid(bad)


def test_extract_managed_metadata_accepts_self_consistent_event():
    key = instance_key("dept-a", "src-1", RecurrenceKey.single())
    uid = target_uid(key)
    vevents = parse_vevents(_managed_ics(uid))
    meta = extract_managed_metadata("https://x/target/a.ics", '"etag1"', vevents)
    assert meta is not None
    assert meta.target_uid == uid
    assert meta.source_id == "dept-a"
    assert meta.instance_key_hint == key


def test_extract_managed_metadata_rejects_missing_managed_flag():
    ics = _managed_ics(target_uid(instance_key("dept-a", "src-1", RecurrenceKey.single())))
    ics = ics.replace("X-CALMIRROR-MANAGED:1\n", "")
    vevents = parse_vevents(ics)
    assert extract_managed_metadata("href", "etag", vevents) is None


def test_extract_managed_metadata_rejects_inconsistent_provenance():
    """UID claims to belong to src-1, but the SOURCE-UID property claims
    src-999: the derived UUID no longer matches the UID, so this must
    never be trusted as ours - even though X-CALMIRROR-MANAGED is set."""
    uid = target_uid(instance_key("dept-a", "src-1", RecurrenceKey.single()))
    ics = _managed_ics(uid, source_uid="src-999")
    vevents = parse_vevents(ics)
    assert extract_managed_metadata("href", "etag", vevents) is None


def test_extract_managed_metadata_rejects_foreign_uuid_with_managed_flag():
    """An attacker-crafted event could set X-CALMIRROR-MANAGED:1 on any
    UID. Ownership must still require the derivable-UUID self-consistency
    check, not just the flag's presence."""
    ics = _managed_ics("00000000-0000-0000-0000-000000000000@calendar-mirror")
    vevents = parse_vevents(ics)
    assert extract_managed_metadata("href", "etag", vevents) is None


def test_extract_managed_metadata_none_for_no_vevents():
    assert extract_managed_metadata("href", "etag", []) is None
