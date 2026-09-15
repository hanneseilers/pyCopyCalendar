from datetime import datetime, timezone

from calendar_sync.config import MirroringConfig
from calendar_sync.ical import parse_vevents
from calendar_sync.models import target_uid
from calendar_sync.recurrence import expand_source_object
from calendar_sync.transform import build_target_event

WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 2, 1, tzinfo=timezone.utc)

DEFAULT_MIRRORING = MirroringConfig(
    copy_description=True, copy_url=True, copy_categories=True,
    copy_alarms=False, copy_attendees=False, copy_organizer=False,
)


def one_occurrence(ics):
    vevents = parse_vevents(ics)
    occs = expand_source_object("dept-a", "https://x/o.ics", '"e1"', vevents, WINDOW_START, WINDOW_END, max_expansion_count=100)
    return occs[0]


FULL_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ev-1
DTSTART:20260110T090000Z
DTEND:20260110T100000Z
SUMMARY:Team sync
LOCATION:Office Berlin
DESCRIPTION:Confidential agenda
URL:https://intranet.example.invalid/meeting
CATEGORIES:Internal,Confidential
ORGANIZER:mailto:boss@example.invalid
ATTENDEE:mailto:alice@example.invalid
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VEVENT
END:VCALENDAR
"""


def build(occurrence, canonical="Berlin Office", mirroring=DEFAULT_MIRRORING, now=None):
    uid = target_uid(occurrence.instance_key)
    return build_target_event(occurrence, canonical, mirroring, uid, now=now)


def test_organizer_attendee_alarm_excluded_by_default():
    occ = one_occurrence(FULL_ICS)
    desired = build(occ)
    ics = desired.ics_bytes.decode()
    assert "ORGANIZER" not in ics
    assert "ATTENDEE" not in ics
    assert "VALARM" not in ics
    assert "METHOD" not in ics


def test_optional_fields_copied_when_enabled():
    occ = one_occurrence(FULL_ICS)
    desired = build(occ)
    ics = desired.ics_bytes.decode()
    assert "DESCRIPTION:Confidential agenda" in ics
    assert "URL:https://intranet.example.invalid/meeting" in ics
    assert "Internal" in ics and "Confidential" in ics


def test_optional_fields_excluded_when_disabled():
    mirroring = MirroringConfig(
        copy_description=False, copy_url=False, copy_categories=False,
        copy_alarms=False, copy_attendees=False, copy_organizer=False,
    )
    occ = one_occurrence(FULL_ICS)
    desired = build(occ, mirroring=mirroring)
    ics = desired.ics_bytes.decode()
    assert "DESCRIPTION" not in ics
    assert "URL:" not in ics
    assert "CATEGORIES" not in ics


def test_organizer_attendee_alarm_copied_when_opted_in():
    mirroring = MirroringConfig(
        copy_description=True, copy_url=True, copy_categories=True,
        copy_alarms=True, copy_attendees=True, copy_organizer=True,
    )
    occ = one_occurrence(FULL_ICS)
    desired = build(occ, mirroring=mirroring)
    ics = desired.ics_bytes.decode()
    assert "ORGANIZER:mailto:boss@example.invalid" in ics
    assert "ATTENDEE:mailto:alice@example.invalid" in ics
    assert "BEGIN:VALARM" in ics


def test_strip_fields_extension_drops_named_field():
    mirroring = MirroringConfig(
        copy_description=True, copy_url=True, copy_categories=True,
        copy_alarms=False, copy_attendees=False, copy_organizer=False,
        strip_fields=("SUMMARY",),
    )
    occ = one_occurrence(FULL_ICS)
    desired = build(occ, mirroring=mirroring)
    assert "SUMMARY:Team sync" not in desired.ics_bytes.decode()


def test_location_uses_effective_raw_text_not_canonical_label():
    occ = one_occurrence(FULL_ICS)  # LOCATION:Office Berlin, canonical is "Berlin Office"
    desired = build(occ, canonical="Berlin Office")
    assert "LOCATION:Office Berlin" in desired.ics_bytes.decode()
    assert desired.canonical_location == "Berlin Office"


def test_target_uid_is_deterministic_and_never_equals_source_uid():
    occ = one_occurrence(FULL_ICS)
    desired = build(occ)
    ics_lines = desired.ics_bytes.decode().splitlines()
    assert "UID:ev-1" not in ics_lines  # exact-line check: the source UID must not become the target UID
    assert desired.target_uid in desired.ics_bytes.decode()


def test_fingerprint_stable_across_rebuilds():
    occ = one_occurrence(FULL_ICS)
    a = build(occ)
    b = build(occ)
    assert a.fingerprint == b.fingerprint


def test_fingerprint_changes_when_summary_changes():
    occ = one_occurrence(FULL_ICS)
    changed_ics = FULL_ICS.replace("Team sync", "Team sync (rescheduled)")
    occ2 = one_occurrence(changed_ics)
    a = build(occ)
    b = build(occ2)
    assert a.fingerprint != b.fingerprint


def test_fingerprint_unaffected_by_dtstamp_or_now():
    occ = one_occurrence(FULL_ICS)
    a = build(occ, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = build(occ, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert a.fingerprint == b.fingerprint


def test_managed_provenance_properties_present():
    occ = one_occurrence(FULL_ICS)
    desired = build(occ)
    ics = desired.ics_bytes.decode()
    assert "X-CALMIRROR-MANAGED:1" in ics
    assert "X-CALMIRROR-SOURCE:dept-a" in ics
    assert "X-CALMIRROR-SOURCE-UID:ev-1" in ics
    assert "X-CALMIRROR-RECURRENCE-ID:SINGLE" in ics
    assert "X-CALMIRROR-FINGERPRINT:" in ics
