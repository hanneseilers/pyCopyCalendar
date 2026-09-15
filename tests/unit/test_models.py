import uuid

from calendar_sync.models import RecurrenceKey, instance_key, target_uid


def test_recurrence_key_single_canonical():
    assert RecurrenceKey.single().canonical() == "SINGLE"


def test_recurrence_key_date_canonical():
    key = RecurrenceKey(kind="DATE", value="20260115")
    assert key.canonical() == "DATE:20260115"


def test_recurrence_key_tzid_canonical():
    key = RecurrenceKey(kind="DATE-TIME", value="20260112T090000", tzid="Europe/Berlin")
    assert key.canonical() == "DATE-TIME;TZID=Europe/Berlin:20260112T090000"


def test_recurrence_key_utc_canonical():
    key = RecurrenceKey(kind="DATE-TIME", value="20260112T090000Z", utc=True)
    assert key.canonical() == "DATE-TIME;UTC:20260112T090000Z"


def test_recurrence_key_floating_canonical():
    key = RecurrenceKey(kind="DATE-TIME", value="20260112T090000")
    assert key.canonical() == "DATE-TIME;FLOATING:20260112T090000"


def test_instance_key_is_stable_string():
    key = instance_key("dept-a", "uid-123", RecurrenceKey.single())
    assert key == "dept-a|uid-123|SINGLE"


def test_instance_key_distinguishes_equal_uids_from_different_sources():
    key_a = instance_key("dept-a", "shared-uid", RecurrenceKey.single())
    key_b = instance_key("dept-b", "shared-uid", RecurrenceKey.single())
    assert key_a != key_b
    assert target_uid(key_a) != target_uid(key_b)


def test_target_uid_is_deterministic():
    key = instance_key("dept-a", "uid-123", RecurrenceKey.single())
    assert target_uid(key) == target_uid(key)
    uid_part = target_uid(key).split("@", 1)[0]
    assert uuid.UUID(uid_part)  # is a syntactically valid UUID


def test_target_uid_has_calendar_mirror_suffix():
    key = instance_key("dept-a", "uid-123", RecurrenceKey.single())
    assert target_uid(key).endswith("@calendar-mirror")


def test_different_recurrence_instances_get_different_target_uids():
    base = instance_key("dept-a", "series-1", RecurrenceKey.single())
    other = instance_key(
        "dept-a", "series-1", RecurrenceKey(kind="DATE-TIME", value="20260112T090000", tzid="Europe/Berlin")
    )
    assert target_uid(base) != target_uid(other)
