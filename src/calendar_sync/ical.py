"""iCalendar parsing and provenance metadata extraction.

Kept separate from recurrence.py (expansion) and transform.py
(target-component construction) so parsing failures are handled in one
place: a malformed calendar object must raise, never be silently treated
as "no events" (NFR-012).
"""

from __future__ import annotations

import re
import uuid

from icalendar import Calendar as ICalendar
from icalendar import Event as ICalEvent

from .models import (
    FINGERPRINT_PROP,
    MANAGED_PROP,
    RECURRENCE_ID_PROP,
    SOURCE_PROP,
    SOURCE_UID_PROP,
    ManagedTargetEvent,
    target_uid as compute_target_uid,
)

_TARGET_UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}@calendar-mirror$"
)


class IcalParseError(ValueError):
    """A calendar object could not be parsed. Callers must treat this as a
    source-read failure (abort, no target mutation), not as zero events."""


def parse_vevents(ics_text: str) -> list[ICalEvent]:
    if not ics_text or not ics_text.strip():
        raise IcalParseError("Calendar object body is empty.")
    try:
        parsed = ICalendar.from_ical(ics_text)
    except Exception as exc:
        raise IcalParseError(f"Could not parse calendar object: {exc}") from exc
    return list(parsed.walk("VEVENT"))


def get_master(vevents: list[ICalEvent]) -> ICalEvent:
    """The component with no RECURRENCE-ID, i.e. the recurring series'
    master or a non-recurring event's only component."""
    for v in vevents:
        if "RECURRENCE-ID" not in v:
            return v
    return vevents[0]


def is_valid_target_uid(uid: str) -> bool:
    """True if `uid` has the shape this application generates
    (uuid5(APPLICATION_NAMESPACE, ...)+"@calendar-mirror") - a real UUID,
    not merely a string that happens to match the suffix."""
    if not _TARGET_UID_RE.match(uid or ""):
        return False
    try:
        uuid.UUID(uid.split("@", 1)[0])
    except ValueError:
        return False
    return True


def extract_managed_metadata(href: str, etag: str | None, vevents: list[ICalEvent]) -> ManagedTargetEvent | None:
    """Returns provenance for a managed target event, or None if the
    object is unmanaged (no X-CALMIRROR-MANAGED) or lacks the ownership
    metadata required before it may ever be auto-deleted (spec section 9):
    a UID matching our namespace alone is not sufficient proof.
    """
    if not vevents:
        return None
    master = get_master(vevents)
    if str(master.get(MANAGED_PROP, "")) != "1":
        return None

    uid = str(master.get("UID", ""))
    source_id = master.get(SOURCE_PROP)
    source_uid = master.get(SOURCE_UID_PROP)
    recurrence_key_raw = master.get(RECURRENCE_ID_PROP)
    fingerprint = master.get(FINGERPRINT_PROP)

    if not (uid and source_id and source_uid and recurrence_key_raw and fingerprint):
        return None  # managed flag present but incomplete provenance: not safe to trust
    if not is_valid_target_uid(uid):
        return None

    # Ownership is only valid if the UID is actually the UUIDv5 derived
    # from this event's own declared provenance - not merely "some UUID
    # in our namespace". This is what lets a plain SQLite row be treated
    # as supporting evidence rather than the sole proof of ownership
    # (spec section 9): the event itself carries a self-verifying claim.
    claimed_key = f"{source_id}|{source_uid}|{recurrence_key_raw}"
    if compute_target_uid(claimed_key) != uid:
        return None

    return ManagedTargetEvent(
        href=href,
        etag=etag,
        target_uid=uid,
        source_id=str(source_id),
        source_uid=str(source_uid),
        recurrence_key_raw=str(recurrence_key_raw),
        fingerprint=str(fingerprint),
        vevent=master,
    )
