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
from datetime import datetime, timezone

from icalendar import Calendar as ICalendar
from icalendar import Event as ICalEvent
from icalendar import vText

from .config import MirroringConfig
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


def build_target_event(
    occurrence: RawOccurrence,
    canonical_location: str,
    config: MirroringConfig,
    target_uid: str,
    *,
    now: datetime | None = None,
) -> DesiredInstance:
    """Transform one eligible RawOccurrence into a standalone target
    VEVENT and its semantic fingerprint."""
    now = now or datetime.now(timezone.utc)
    src = occurrence.vevent
    stripped = set(config.strip_fields) | _HARD_EXCLUDED
    if not config.copy_organizer:
        stripped.add("ORGANIZER")
    if not config.copy_attendees:
        stripped.add("ATTENDEE")
    # VALARM is a subcomponent, not a property, so it isn't part of
    # `stripped` - copy_alarms alone gates it, in the loop below.

    event = ICalEvent()
    event.add("UID", target_uid)
    event.add("DTSTAMP", now)

    canonical_fields: dict = {"summary": str(src.get("SUMMARY", "")), "location": occurrence.location}
    if "SUMMARY" not in stripped:
        event.add("SUMMARY", src.get("SUMMARY", vText("")))

    event.add("DTSTART", occurrence.start)
    canonical_fields["dtstart"] = _dt_field(occurrence.start)
    dtend_prop = src.get("DTEND")
    if dtend_prop is not None:
        event.add("DTEND", occurrence.end)
    else:
        duration = src.get("DURATION")
        if duration is not None and "DURATION" not in stripped:
            event.add("DURATION", duration)
    canonical_fields["dtend"] = _dt_field(occurrence.end)

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
            value = src.get(name)
            canonical_fields[name.lower()] = sorted(str(v) for v in value) if isinstance(value, list) else str(value)

    for name in ("CLASS", "TRANSP"):
        if name not in stripped and src.get(name) is not None:
            event.add(name, src.get(name))
            canonical_fields[name.lower()] = str(src.get(name))

    if config.copy_organizer and "ORGANIZER" not in stripped and src.get("ORGANIZER") is not None:
        event.add("ORGANIZER", src.get("ORGANIZER"))
        canonical_fields["organizer"] = str(src.get("ORGANIZER"))

    if config.copy_attendees and "ATTENDEE" not in stripped and src.get("ATTENDEE") is not None:
        _copy_all(src, event, "ATTENDEE")
        value = src.get("ATTENDEE")
        canonical_fields["attendees"] = sorted(str(v) for v in value) if isinstance(value, list) else [str(value)]

    if config.copy_alarms:
        alarm_fingerprints = []
        for sub in src.subcomponents:
            if sub.name == "VALARM":
                event.add_component(sub)
                alarm_fingerprints.append(
                    {"action": str(sub.get("ACTION", "")), "trigger": str(sub.get("TRIGGER", ""))}
                )
        if alarm_fingerprints:
            canonical_fields["alarms"] = alarm_fingerprints

    fingerprint = hashlib.sha256(
        json.dumps(canonical_fields, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()

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
        start=occurrence.start,
        end=occurrence.end,
        all_day=occurrence.all_day,
        summary=str(src.get("SUMMARY", "")),
        ics_bytes=ics_bytes,
        fingerprint=fingerprint,
    )
