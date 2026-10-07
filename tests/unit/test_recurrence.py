from datetime import date, datetime, timezone

import pytest

from calendar_sync.ical import parse_vevents
from calendar_sync.recurrence import RecurrenceExpansionError, expand_source_object

WINDOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 2, 1, tzinfo=timezone.utc)


def expand(ics, **kw):
    vevents = parse_vevents(ics)
    return expand_source_object(
        "dept-a", "https://x/obj.ics", '"e1"', vevents, WINDOW_START, WINDOW_END,
        max_expansion_count=kw.pop("max_expansion_count", 1000),
    )


def test_single_non_recurring_event_gets_single_key():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:single-1
DTSTART:20260110T120000Z
DTEND:20260110T130000Z
SUMMARY:Single event
LOCATION:Berlin Office
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert len(occs) == 1
    assert occs[0].recurrence_key.canonical() == "SINGLE"
    assert occs[0].cancelled is False


def test_weekly_series_expands_within_window():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-1
DTSTART;TZID=Europe/Berlin:20260105T090000
DTEND;TZID=Europe/Berlin:20260105T100000
SUMMARY:Weekly
LOCATION:Berlin Office
RRULE:FREQ=WEEKLY;COUNT=4
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert len(occs) == 4
    assert all(o.recurrence_key.kind == "DATE-TIME" for o in occs)
    assert all(o.recurrence_key.tzid == "Europe/Berlin" for o in occs)


def test_exdate_excludes_one_occurrence():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-2
DTSTART;TZID=Europe/Berlin:20260105T090000
DTEND;TZID=Europe/Berlin:20260105T100000
SUMMARY:Weekly
LOCATION:Berlin Office
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=Europe/Berlin:20260112T090000
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert len(occs) == 3
    starts = {o.start for o in occs}
    assert all(s.day != 12 for s in starts)


def test_moved_exception_preserves_original_recurrence_identity():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-3
DTSTART;TZID=Europe/Berlin:20260105T090000
DTEND;TZID=Europe/Berlin:20260105T100000
SUMMARY:Weekly
LOCATION:Berlin Office
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:series-3
RECURRENCE-ID;TZID=Europe/Berlin:20260112T090000
DTSTART;TZID=Europe/Berlin:20260114T150000
DTEND;TZID=Europe/Berlin:20260114T160000
SUMMARY:Moved
LOCATION:Home Office
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert len(occs) == 3
    moved = next(o for o in occs if o.location == "Home Office")
    assert moved.recurrence_key.value == "20260112T090000"  # original slot, not the moved date
    assert moved.start.day == 14  # DTSTART itself did move


def test_cancelled_exception_is_flagged_not_dropped():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-4
DTSTART;TZID=Europe/Berlin:20260105T090000
DTEND;TZID=Europe/Berlin:20260105T100000
SUMMARY:Weekly
LOCATION:Berlin Office
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:series-4
RECURRENCE-ID;TZID=Europe/Berlin:20260112T090000
DTSTART;TZID=Europe/Berlin:20260112T090000
DTEND;TZID=Europe/Berlin:20260112T100000
SUMMARY:Cancelled
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert len(occs) == 2
    cancelled = [o for o in occs if o.cancelled]
    assert len(cancelled) == 1


def test_all_day_event_uses_date_kind():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:allday-1
DTSTART;VALUE=DATE:20260115
DTEND;VALUE=DATE:20260116
SUMMARY:All day
LOCATION:Berlin Office
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert len(occs) == 1
    assert occs[0].all_day is True
    assert isinstance(occs[0].start, date) and not isinstance(occs[0].start, datetime)


def test_floating_time_has_no_tzinfo():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:floating-1
DTSTART:20260110T140000
DTEND:20260110T150000
SUMMARY:Floating
LOCATION:Berlin Office
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert occs[0].start.tzinfo is None
    assert occs[0].recurrence_key.canonical() == "SINGLE"  # non-recurring, no RRULE


def test_utc_time_recurrence_key_when_recurring():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-utc
DTSTART:20260105T090000Z
DTEND:20260105T100000Z
SUMMARY:Weekly UTC
LOCATION:Berlin Office
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
END:VCALENDAR
"""
    occs = expand(ics)
    assert all(o.recurrence_key.utc for o in occs)
    assert all(o.recurrence_key.canonical().startswith("DATE-TIME;UTC:") for o in occs)


def test_dst_transition_in_europe_berlin_is_handled():
    """2026-03-29 is the spring-forward DST transition in Europe/Berlin;
    the expansion must not crash or silently drop/duplicate occurrences
    around it."""
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-dst
DTSTART;TZID=Europe/Berlin:20260322T090000
DTEND;TZID=Europe/Berlin:20260322T100000
SUMMARY:Weekly across DST
LOCATION:Berlin Office
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
END:VCALENDAR
"""
    vevents = parse_vevents(ics)
    occs = expand_source_object(
        "dept-a", "https://x/obj.ics", '"e1"', vevents,
        datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 4, 15, tzinfo=timezone.utc),
        max_expansion_count=1000,
    )
    assert len(occs) == 3
    hours = {o.start.hour for o in occs}
    assert hours == {9}  # local wall-clock time preserved across the DST boundary


def test_max_expansion_count_is_enforced():
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-huge
DTSTART:20260101T000000Z
DTEND:20260101T001000Z
SUMMARY:Daily
LOCATION:Berlin Office
RRULE:FREQ=DAILY;COUNT=1000
END:VEVENT
END:VCALENDAR
"""
    with pytest.raises(RecurrenceExpansionError):
        expand(ics, max_expansion_count=5)


def test_multiple_source_uids_produce_distinct_instance_keys():
    ics_a = parse_vevents(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:shared-uid\nDTSTART:20260110T090000Z\n"
        "DTEND:20260110T100000Z\nSUMMARY:A\nLOCATION:Berlin Office\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    occs_a = expand_source_object("dept-a", "href-a", "e1", ics_a, WINDOW_START, WINDOW_END, max_expansion_count=10)
    occs_b = expand_source_object("dept-b", "href-b", "e1", ics_a, WINDOW_START, WINDOW_END, max_expansion_count=10)
    assert occs_a[0].instance_key != occs_b[0].instance_key
