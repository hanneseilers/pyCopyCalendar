"""Build the standalone target VEVENT from an explicit allowlist.

TECHNICAL_SPECIFICATION.md section 13 requires a *fresh* component built
field-by-field, never a clone of the source component: ORGANIZER,
ATTENDEE, METHOD, VALARM and ATTACH must never leak into the target by
default, since that would turn the mirror into an accidental invitation
or duplicate-reminder generator. `mirroring.strip_fields` (carried over
and extended from the original project's config) can drop additional
named fields on top of that allowlist.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from icalendar import Calendar as ICalendar
from icalendar import Event as ICalEvent
from icalendar import vText

from .config import BufferConfig, MirroringConfig
from .models import (
    FINGERPRINT_PROP,
    MANAGED_PROP,
    RECURRENCE_ID_PROP,
    SOURCE_PROP,
    SOURCE_UID_PROP,
    DesiredInstance,
    RawOccurrence,
)

# Never copied, regardless of mirroring config - these are the properties
# that would make the mirror behave like a scheduling participant instead
# of a read-only view (ORGANIZER/ATTENDEE/METHOD), duplicate reminders
# (VALARM handled separately, gated by copy_alarms), or leak binary
# payloads (ATTACH). RRULE/RDATE/EXDATE are excluded because every
# occurrence is materialized standalone (section 12.7).
_HARD_EXCLUDED = frozenset({
    "UID", "DTSTAMP", "SEQUENCE", "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID",
    "METHOD", "ATTACH",
})

def _dt_field(value) -> dict:
    if isinstance(value, datetime):
        tzid = None
        utc = False
        if value.tzinfo is not None:
            tzid = getattr(value.tzinfo, "key", None)  # zoneinfo exposes .key; UTC/fixed offsets don't
            if tzid is None:
                utc = value.utcoffset() == timezone.utc.utcoffset(None)
        return {
            "type": "DATE-TIME",
            "value": value.strftime("%Y%m%dT%H%M%S"),
            "tzid": tzid,
            "utc": utc if tzid is None else False,
        }
    return {"type": "DATE", "value": value.strftime("%Y%m%d"), "tzid": None, "utc": False}


def _copy_all(source: ICalEvent, target: ICalEvent, name: str) -> None:
    value = source.get(name)
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            target.add(name, item)
    else:
        target.add(name, value)


def _effective_end(vevent: ICalEvent, start):
    dtend = vevent.get("DTEND")
    if dtend is not None:
        return dtend.dt
    duration = vevent.get("DURATION")
    if duration is not None:
        return start + duration.dt
    return start


def _apply_buffer(start, end, all_day: bool, buffer: BufferConfig):
    """Pad a timed occurrence's start/end for the target calendar copy.
    Never applied to all-day events - a "15 minutes before midnight"
    buffer has no sensible meaning there, so DATE values pass through
    unchanged regardless of configured buffer minutes."""
    if all_day or (not buffer.before_minutes and not buffer.after_minutes):
        return start, end
    if buffer.before_minutes:
        start = start - timedelta(minutes=buffer.before_minutes)
    if buffer.after_minutes:
        end = end + timedelta(minutes=buffer.after_minutes)
    return start, end


def _stripped_fields(config: MirroringConfig) -> set[str]:
    stripped = set(config.strip_fields) | _HARD_EXCLUDED
    if not config.copy_organizer:
        stripped.add("ORGANIZER")
    if not config.copy_attendees:
        stripped.add("ATTENDEE")
    return stripped


def compute_fingerprint(vevent: ICalEvent, config: MirroringConfig) -> str:
    """Semantic fingerprint of exactly the fields that end up in the
    target VEVENT under `config` - and nothing else.

    Deliberately callable on *any* VEVENT, not just a freshly-transformed
    one: reconcile.py also calls this on a target event re-read from the
    server to detect a manual edit that left the object's own
    X-CALMIRROR-FINGERPRINT property untouched. Restricting the fields
    considered to exactly what `config` would copy is what keeps the two
    calls comparable - including a field once, unconditionally, would
    make a stripped/disabled field's absence in the real target register
    as a permanent, spurious "changed" on every run.
    """
    stripped = _stripped_fields(config)
    fields: dict = {}

    if "SUMMARY" not in stripped:
        fields["summary"] = str(vevent.get("SUMMARY", ""))

    start = vevent["DTSTART"].dt
    fields["dtstart"] = _dt_field(start)
    fields["dtend"] = _dt_field(_effective_end(vevent, start))

    if "LOCATION" not in stripped:
        fields["location"] = str(vevent.get("LOCATION", ""))

    for name, enabled in (
        ("DESCRIPTION", config.copy_description),
        ("URL", config.copy_url),
        ("CATEGORIES", config.copy_categories),
    ):
        if enabled and name not in stripped and vevent.get(name) is not None:
            value = vevent.get(name)
            fields[name.lower()] = sorted(str(v) for v in value) if isinstance(value, list) else str(value)

    for name in ("CLASS", "TRANSP"):
        if name not in stripped and vevent.get(name) is not None:
            fields[name.lower()] = str(vevent.get(name))

    if config.copy_organizer and "ORGANIZER" not in stripped and vevent.get("ORGANIZER") is not None:
        fields["organizer"] = str(vevent.get("ORGANIZER"))

    if config.copy_attendees and "ATTENDEE" not in stripped and vevent.get("ATTENDEE") is not None:
        value = vevent.get("ATTENDEE")
        fields["attendees"] = sorted(str(v) for v in value) if isinstance(value, list) else [str(value)]

    if config.copy_alarms:
        alarms = [
            {"action": str(sub.get("ACTION", "")), "trigger": str(sub.get("TRIGGER", ""))}
            for sub in vevent.subcomponents
            if sub.name == "VALARM"
        ]
        if alarms:
            fields["alarms"] = alarms

    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def build_target_event(
    occurrence: RawOccurrence,
    canonical_location: str,
    config: MirroringConfig,
    target_uid: str,
    *,
    buffer: BufferConfig | None = None,
    now: datetime | None = None,
) -> DesiredInstance:
    """Transform one eligible RawOccurrence into a standalone target
    VEVENT and its semantic fingerprint."""
    now = now or datetime.now(timezone.utc)
    buffer = buffer or BufferConfig()
    src = occurrence.vevent
    stripped = _stripped_fields(config)

    buffered_start, buffered_end = _apply_buffer(
        occurrence.start, occurrence.end, occurrence.all_day, buffer
    )
    buffered = buffered_start != occurrence.start or buffered_end != occurrence.end

    event = ICalEvent()
    event.add("UID", target_uid)
    event.add("DTSTAMP", now)

    if "SUMMARY" not in stripped:
        event.add("SUMMARY", src.get("SUMMARY", vText("")))

    event.add("DTSTART", buffered_start)
    dtend_prop = src.get("DTEND")
    if dtend_prop is not None or buffered:
        # A source using DURATION instead of DTEND still gets an explicit
        # DTEND once buffering changes the interval - preserving DURATION
        # unmodified alongside a shifted DTSTART would silently un-do the
        # "after" padding (DURATION is relative to DTSTART, not fixed).
        event.add("DTEND", buffered_end)
    else:
        duration = src.get("DURATION")
        if duration is not None and "DURATION" not in stripped:
            event.add("DURATION", duration)

    if "LOCATION" not in stripped:
        event.add("LOCATION", occurrence.location)

    optional_toggle_fields = {
        "DESCRIPTION": config.copy_description,
        "URL": config.copy_url,
        "CATEGORIES": config.copy_categories,
    }
    for name, enabled in optional_toggle_fields.items():
        if enabled and name not in stripped and src.get(name) is not None:
            _copy_all(src, event, name)

    for name in ("CLASS", "TRANSP"):
        if name not in stripped and src.get(name) is not None:
            event.add(name, src.get(name))

    if config.copy_organizer and "ORGANIZER" not in stripped and src.get("ORGANIZER") is not None:
        event.add("ORGANIZER", src.get("ORGANIZER"))

    if config.copy_attendees and "ATTENDEE" not in stripped and src.get("ATTENDEE") is not None:
        _copy_all(src, event, "ATTENDEE")

    if config.copy_alarms:
        # Copied as-is: a TRIGGER relative to DTSTART now counts from the
        # buffered DTSTART, not the original occurrence time, if a buffer
        # is configured.
        for sub in src.subcomponents:
            if sub.name == "VALARM":
                event.add_component(sub)

    # Fingerprint from `event` itself, not `src`: its DTSTART/DTEND/
    # LOCATION/etc are exactly what gets written to the target (buffered
    # times included), so recomputing this same function later from a
    # *re-read* target event - see reconcile.py's tamper-detection call -
    # agrees with what was written here, rather than with the source's
    # unbuffered original values.
    fingerprint = compute_fingerprint(event, config)

    event.add(MANAGED_PROP, "1")
    event.add(SOURCE_PROP, occurrence.source_id)
    event.add(SOURCE_UID_PROP, occurrence.source_uid)
    event.add(RECURRENCE_ID_PROP, occurrence.recurrence_key.canonical())
    event.add(FINGERPRINT_PROP, fingerprint)

    cal = ICalendar()
    cal.add("prodid", "-//calendar-sync//mirror//")
    cal.add("version", "2.0")
    cal.add_component(event)
    ics_bytes = cal.to_ical()

    return DesiredInstance(
        source_id=occurrence.source_id,
        source_uid=occurrence.source_uid,
        recurrence_key=occurrence.recurrence_key,
        source_href=occurrence.source_href,
        source_etag=occurrence.source_etag,
        canonical_location=canonical_location,
        start=buffered_start,
        end=buffered_end,
        all_day=occurrence.all_day,
        summary=str(src.get("SUMMARY", "")),
        ics_bytes=ics_bytes,
        fingerprint=fingerprint,
    )
