from datetime import datetime, timezone

from calendar_sync.config import BufferConfig, MirroringConfig, SummaryOverrideConfig
from calendar_sync.ical import parse_vevents
from calendar_sync.models import target_uid
from calendar_sync.recurrence import expand_source_object
from calendar_sync.transform import build_target_event, compute_fingerprint

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


def build(occurrence, canonical="Berlin Office", mirroring=DEFAULT_MIRRORING, now=None, buffer=None, summary_override=None):
    uid = target_uid(occurrence.instance_key)
    return build_target_event(
        occurrence, canonical, mirroring, uid, now=now, buffer=buffer, summary_override=summary_override
    )


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


# -- buffer ------------------------------------------------------------

TIMED_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ev-buf
DTSTART;TZID=Europe/Berlin:20260110T100000
DTEND;TZID=Europe/Berlin:20260110T110000
SUMMARY:Meeting
LOCATION:Berlin Office
END:VEVENT
END:VCALENDAR
"""

ALL_DAY_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ev-allday
DTSTART;VALUE=DATE:20260115
DTEND;VALUE=DATE:20260116
SUMMARY:All day
LOCATION:Berlin Office
END:VEVENT
END:VCALENDAR
"""

DURATION_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ev-dur
DTSTART:20260110T100000Z
DURATION:PT1H
SUMMARY:Duration based
LOCATION:Berlin Office
END:VEVENT
END:VCALENDAR
"""


def test_buffer_pads_start_and_end_of_timed_event():
    occ = one_occurrence(TIMED_ICS)
    desired = build(occ, buffer=BufferConfig(before_minutes=15, after_minutes=30))
    assert desired.start.strftime("%H:%M") == "09:45"
    assert desired.end.strftime("%H:%M") == "11:30"
    ics = desired.ics_bytes.decode()
    assert "DTSTART;TZID=Europe/Berlin:20260110T094500" in ics
    assert "DTEND;TZID=Europe/Berlin:20260110T113000" in ics


def test_no_buffer_leaves_original_times():
    occ = one_occurrence(TIMED_ICS)
    desired = build(occ)  # buffer=None -> BufferConfig() defaults to 0/0
    assert desired.start.strftime("%H:%M") == "10:00"
    assert desired.end.strftime("%H:%M") == "11:00"


def test_buffer_only_before_or_only_after():
    occ = one_occurrence(TIMED_ICS)
    before_only = build(occ, buffer=BufferConfig(before_minutes=10, after_minutes=0))
    assert before_only.start.strftime("%H:%M") == "09:50"
    assert before_only.end.strftime("%H:%M") == "11:00"

    after_only = build(occ, buffer=BufferConfig(before_minutes=0, after_minutes=20))
    assert after_only.start.strftime("%H:%M") == "10:00"
    assert after_only.end.strftime("%H:%M") == "11:20"


def test_buffer_not_applied_to_all_day_events():
    occ = one_occurrence(ALL_DAY_ICS)
    desired = build(occ, buffer=BufferConfig(before_minutes=15, after_minutes=30))
    assert desired.start.strftime("%Y%m%d") == "20260115"
    assert desired.end.strftime("%Y%m%d") == "20260116"
    assert desired.all_day is True


def test_buffer_converts_duration_based_event_to_explicit_dtend():
    occ = one_occurrence(DURATION_ICS)
    desired = build(occ, buffer=BufferConfig(before_minutes=15, after_minutes=30))
    ics = desired.ics_bytes.decode()
    assert "DTEND:20260110T113000Z" in ics
    assert "DURATION" not in ics


def test_buffer_is_idempotent_across_rebuilds():
    occ = one_occurrence(TIMED_ICS)
    buf = BufferConfig(before_minutes=15, after_minutes=30)
    a = build(occ, buffer=buf)
    b = build(occ, buffer=buf)
    assert a.fingerprint == b.fingerprint
    assert a.start == b.start and a.end == b.end


def test_buffer_changes_fingerprint_vs_unbuffered():
    occ = one_occurrence(TIMED_ICS)
    unbuffered = build(occ)
    buffered = build(occ, buffer=BufferConfig(before_minutes=15, after_minutes=30))
    assert unbuffered.fingerprint != buffered.fingerprint


def test_fingerprint_recomputed_from_written_target_matches_buffered_original():
    """Regression guard: change detection re-derives the fingerprint from
    the target's actual re-read VEVENT (see reconcile.py). If that VEVENT
    carries the buffered DTSTART/DTEND (as written), recomputing from it
    must reproduce exactly the fingerprint stored at write time - or every
    run would see a spurious, permanent "changed" for buffered events."""
    occ = one_occurrence(TIMED_ICS)
    desired = build(occ, buffer=BufferConfig(before_minutes=15, after_minutes=30))
    written_back_vevents = parse_vevents(desired.ics_bytes.decode())
    recomputed = compute_fingerprint(written_back_vevents[0], DEFAULT_MIRRORING)
    assert recomputed == desired.fingerprint


# -- summary_override ---------------------------------------------------

CONFIDENTIAL_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ev-confidential
DTSTART:20260110T100000Z
DTEND:20260110T110000Z
SUMMARY:Confidential 1:1 with Alice
DESCRIPTION:Discuss salary
LOCATION:Berlin Office
END:VEVENT
END:VCALENDAR
"""

OVERRIDE = SummaryOverrideConfig(enabled=True, replacement_text="Busy")


def test_summary_override_disabled_leaves_title_and_description_unchanged():
    occ = one_occurrence(CONFIDENTIAL_ICS)
    desired = build(occ)  # summary_override=None -> disabled
    ics = desired.ics_bytes.decode()
    assert "SUMMARY:Confidential 1:1 with Alice" in ics
    assert "DESCRIPTION:Discuss salary" in ics


def test_summary_override_replaces_title_and_prepends_original_to_description():
    occ = one_occurrence(CONFIDENTIAL_ICS)
    desired = build(occ, summary_override=OVERRIDE)
    ics = desired.ics_bytes.decode()
    assert "SUMMARY:Busy" in ics
    assert "Confidential 1:1 with Alice" not in ics.split("DESCRIPTION:", 1)[0]
    description = ics.split("DESCRIPTION:", 1)[1]
    assert description.startswith("Confidential 1:1 with Alice")
    assert "Discuss salary" in description


def test_summary_override_operator_facing_summary_field_keeps_real_title():
    """DesiredInstance.summary is used for operator-facing log lines, not
    written into the target calendar - it must keep showing the real
    title regardless of the override."""
    occ = one_occurrence(CONFIDENTIAL_ICS)
    desired = build(occ, summary_override=OVERRIDE)
    assert desired.summary == "Confidential 1:1 with Alice"


def test_summary_override_without_description_copy_still_preserves_original_title():
    mirroring = MirroringConfig(
        copy_description=False, copy_url=True, copy_categories=True,
        copy_alarms=False, copy_attendees=False, copy_organizer=False,
    )
    occ = one_occurrence(CONFIDENTIAL_ICS)
    desired = build(occ, mirroring=mirroring, summary_override=OVERRIDE)
    ics = desired.ics_bytes.decode()
    assert "SUMMARY:Busy" in ics
    assert "Confidential 1:1 with Alice" in ics
    assert "Discuss salary" not in ics  # real description correctly still excluded


def test_summary_override_respects_description_strip_field():
    """An operator who explicitly stripped DESCRIPTION gets no DESCRIPTION
    at all - not even the original title - since strip_fields is a
    stronger, more specific directive than summary_override's default
    behavior."""
    mirroring = MirroringConfig(
        copy_description=True, copy_url=True, copy_categories=True,
        copy_alarms=False, copy_attendees=False, copy_organizer=False,
        strip_fields=("DESCRIPTION",),
    )
    occ = one_occurrence(CONFIDENTIAL_ICS)
    desired = build(occ, mirroring=mirroring, summary_override=OVERRIDE)
    ics = desired.ics_bytes.decode()
    assert "SUMMARY:Busy" in ics
    assert "DESCRIPTION" not in ics


def test_summary_override_is_idempotent():
    occ = one_occurrence(CONFIDENTIAL_ICS)
    a = build(occ, summary_override=OVERRIDE)
    b = build(occ, summary_override=OVERRIDE)
    assert a.fingerprint == b.fingerprint


def test_summary_override_fingerprint_roundtrip_from_written_target():
    occ = one_occurrence(CONFIDENTIAL_ICS)
    desired = build(occ, summary_override=OVERRIDE)
    written_back = parse_vevents(desired.ics_bytes.decode())
    recomputed = compute_fingerprint(written_back[0], DEFAULT_MIRRORING, OVERRIDE)
    assert recomputed == desired.fingerprint


def test_summary_override_detects_source_rename_even_with_copy_description_off():
    """Regression guard: with SUMMARY forced to a constant, DESCRIPTION is
    the only field left that can reflect a source title change - it must
    therefore always participate in the fingerprint when the override is
    active, even if mirroring.copy_description is off."""
    mirroring = MirroringConfig(
        copy_description=False, copy_url=True, copy_categories=True,
        copy_alarms=False, copy_attendees=False, copy_organizer=False,
    )
    occ_a = one_occurrence(CONFIDENTIAL_ICS)
    occ_b = one_occurrence(CONFIDENTIAL_ICS.replace("with Alice", "with Bob"))
    a = build(occ_a, mirroring=mirroring, summary_override=OVERRIDE)
    b = build(occ_b, mirroring=mirroring, summary_override=OVERRIDE)
    assert a.fingerprint != b.fingerprint


def test_summary_override_enabled_still_writes_managed_provenance():
    occ = one_occurrence(CONFIDENTIAL_ICS)
    desired = build(occ, summary_override=OVERRIDE)
    ics = desired.ics_bytes.decode()
    assert "X-CALMIRROR-MANAGED:1" in ics
    assert "X-CALMIRROR-FINGERPRINT:" in ics
