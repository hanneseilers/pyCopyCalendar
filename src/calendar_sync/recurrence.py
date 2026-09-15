"""Bounded recurrence expansion.

Uses the `recurring_ical_events` library (a mature, actively maintained
expansion engine) instead of hand-rolled RRULE arithmetic, per
TECHNICAL_SPECIFICATION.md section 3 ("Recurring events must not be
expanded with custom RRULE arithmetic"). Nextcloud's own REPORT
calendar-query expansion (`expand` request body) was deliberately not
relied on here because its exact behavior across server versions has not
been verified against a live instance - see the README's note on required
live-server verification.
"""

from __future__ import annotations

from datetime import date, datetime

import recurring_ical_events
from icalendar import Calendar as ICalendar
from icalendar import Event as ICalEvent

from .ical import get_master
from .models import RawOccurrence, RecurrenceKey


class RecurrenceExpansionError(ValueError):
    """Expansion failed or exceeded the configured defensive bound."""


def _is_recurring(master: ICalEvent) -> bool:
    return "RRULE" in master or "RDATE" in master


def _build_recurrence_key(rid_property) -> RecurrenceKey:
    value = rid_property.dt
    tzid = rid_property.params.get("TZID")
    if isinstance(value, datetime):
        if tzid:
            return RecurrenceKey(kind="DATE-TIME", value=value.strftime("%Y%m%dT%H%M%S"), tzid=str(tzid))
        if value.tzinfo is not None:
            # No TZID param but tz-aware: icalendar parses a trailing "Z" as
            # UTC without attaching TZID, so this is the UTC case.
            return RecurrenceKey(kind="DATE-TIME", value=value.strftime("%Y%m%dT%H%M%SZ"), utc=True)
        return RecurrenceKey(kind="DATE-TIME", value=value.strftime("%Y%m%dT%H%M%S"))
    # date (all-day)
    return RecurrenceKey(kind="DATE", value=value.strftime("%Y%m%d"))


def _effective_end(occurrence: ICalEvent, start):
    dtend = occurrence.get("DTEND")
    if dtend is not None:
        return dtend.dt
    duration = occurrence.get("DURATION")
    if duration is not None:
        return start + duration.dt
    return start  # zero-length fallback; DTSTART-only events are valid per RFC 5545


def expand_source_object(
    source_id: str,
    href: str,
    etag: str | None,
    vevents: list[ICalEvent],
    window_start: datetime,
    window_end: datetime,
    *,
    max_expansion_count: int,
) -> list[RawOccurrence]:
    """Expand one source calendar object (its master plus any
    RECURRENCE-ID exceptions) into every occurrence overlapping the
    window, preserving each occurrence's original identity even when an
    exception moved its DTSTART elsewhere."""
    if not vevents:
        return []
    master = get_master(vevents)
    source_uid = str(master.get("UID", ""))
    is_recurring = _is_recurring(master)

    container = ICalendar()
    container.add("prodid", "-//calendar-sync//expansion//")
    container.add("version", "2.0")
    for v in vevents:
        container.add_component(v)

    try:
        occurrences = recurring_ical_events.of(container, components=["VEVENT"]).between(
            window_start, window_end
        )
    except Exception as exc:
        raise RecurrenceExpansionError(
            f"Failed to expand recurrence for source object {href!r}: {exc}"
        ) from exc

    if len(occurrences) > max_expansion_count:
        raise RecurrenceExpansionError(
            f"Source object {href!r} expanded to {len(occurrences)} occurrences, "
            f"exceeding safety.max_expansion_count={max_expansion_count}."
        )

    results: list[RawOccurrence] = []
    for occ in occurrences:
        recurrence_key = (
            RecurrenceKey.single() if not is_recurring else _build_recurrence_key(occ["RECURRENCE-ID"])
        )
        start = occ["DTSTART"].dt
        end = _effective_end(occ, start)
        all_day = isinstance(start, date) and not isinstance(start, datetime)
        cancelled = str(occ.get("STATUS", "")).upper() == "CANCELLED"
        location = str(occ.get("LOCATION") or "")
        results.append(
            RawOccurrence(
                source_id=source_id,
                source_uid=source_uid,
                recurrence_key=recurrence_key,
                source_href=href,
                source_etag=etag,
                vevent=occ,
                location=location,
                start=start,
                end=end,
                all_day=all_day,
                cancelled=cancelled,
            )
        )
    return results
