"""Shared data types: occurrence identity, DAV resources, and plan actions.

See TECHNICAL_SPECIFICATION.md section 9 (event identity and provenance)
for the reasoning behind `RecurrenceKey`, `instance_key` and `target_uid`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Fixed application namespace for deterministic UUIDv5 target UIDs, derived
# once via uuid.uuid5(uuid.NAMESPACE_URL,
# "https://github.com/hanneseilers/pyCopyCalendar/calendar-sync") and
# hardcoded so recreation after database loss is reproducible without
# re-running that derivation.
APPLICATION_NAMESPACE = uuid.UUID("eab7c498-27b8-522e-a8e3-6723b415cd94")

MANAGED_PROP = "X-CALMIRROR-MANAGED"
SOURCE_PROP = "X-CALMIRROR-SOURCE"
SOURCE_UID_PROP = "X-CALMIRROR-SOURCE-UID"
RECURRENCE_ID_PROP = "X-CALMIRROR-RECURRENCE-ID"
FINGERPRINT_PROP = "X-CALMIRROR-FINGERPRINT"


def normalize_etag(value: object) -> str | None:
    """caldav's CalendarObjectResource.etag has returned either a plain
    string or a raw lxml element (holding the getetag property text)
    depending on library version/code path. Normalize defensively at the
    one point every gateway reads an etag, rather than trusting either
    shape blindly."""
    if value is None:
        return None
    text = getattr(value, "text", None)
    if text is not None:
        return text
    return str(value)

SINGLE = "SINGLE"


@dataclass(frozen=True)
class RecurrenceKey:
    """Type-preserving, normalized identity of one recurrence instance.

    `kind` is one of "SINGLE" (non-recurring event), "DATE" (all-day
    RECURRENCE-ID), or "DATE-TIME" (timed RECURRENCE-ID, further qualified
    by `tzid`: None means UTC ("Z") when `utc` is True, or a floating time
    when `utc` is False; a concrete `tzid` means a zoned time).
    """

    kind: str
    value: str = ""
    tzid: str | None = None
    utc: bool = False

    def canonical(self) -> str:
        if self.kind == SINGLE:
            return SINGLE
        if self.kind == "DATE":
            return f"DATE:{self.value}"
        if self.tzid:
            return f"DATE-TIME;TZID={self.tzid}:{self.value}"
        if self.utc:
            return f"DATE-TIME;UTC:{self.value}"
        return f"DATE-TIME;FLOATING:{self.value}"

    @classmethod
    def single(cls) -> "RecurrenceKey":
        return cls(kind=SINGLE)


def instance_key(source_id: str, source_uid: str, recurrence_key: RecurrenceKey) -> str:
    """Stable synchronization-unit identity: source_id|source_uid|recurrence_key."""
    return f"{source_id}|{source_uid}|{recurrence_key.canonical()}"


def target_uid(key: str) -> str:
    """Deterministic target UID for a given instance_key."""
    return f"{uuid.uuid5(APPLICATION_NAMESPACE, key)}@calendar-mirror"


class ChangeType(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    UNCHANGED = "unchanged"
    RECREATE = "recreate"
    QUARANTINE = "quarantine"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class RawOccurrence:
    """One materialized source occurrence (recurrence expansion already
    applied) before location filtering and transformation.

    `vevent` is the fully resolved icalendar Event for this occurrence -
    the master for a non-recurring event, or the merged
    exception/instance component for a recurring one - as produced by
    recurrence.py. `cancelled` is True for a STATUS:CANCELLED exception,
    which must never become a desired instance regardless of location.
    """

    source_id: str
    source_uid: str
    recurrence_key: RecurrenceKey
    source_href: str
    source_etag: str | None
    vevent: object  # icalendar.cal.Event; typed loosely to avoid importing icalendar here
    location: str
    start: datetime
    end: datetime
    all_day: bool
    cancelled: bool = False

    @property
    def instance_key(self) -> str:
        return instance_key(self.source_id, self.source_uid, self.recurrence_key)


@dataclass(frozen=True)
class DesiredInstance:
    """One eligible occurrence, transformed into the standalone target
    VEVENT that should exist in the target calendar."""

    source_id: str
    source_uid: str
    recurrence_key: RecurrenceKey
    source_href: str
    source_etag: str | None
    canonical_location: str
    start: datetime
    end: datetime
    all_day: bool
    summary: str
    ics_bytes: bytes  # standalone VCALENDAR containing the transformed VEVENT
    fingerprint: str

    @property
    def instance_key(self) -> str:
        return instance_key(self.source_id, self.source_uid, self.recurrence_key)

    @property
    def target_uid(self) -> str:
        return target_uid(self.instance_key)


@dataclass(frozen=True)
class CalendarResource:
    """A raw calendar object as read from DAV (source or target)."""

    href: str
    etag: str | None
    ics_text: str


@dataclass(frozen=True)
class ManagedTargetEvent:
    """A target-calendar event carrying valid X-CALMIRROR-* provenance.

    `fingerprint` is the value the event *claims* for itself (its
    X-CALMIRROR-FINGERPRINT property, as last written by this
    application). `vevent` is the event's actual current master
    component, kept so callers can independently recompute a fingerprint
    from its real current field values via transform.compute_fingerprint
    - detecting a manual edit that left the claimed fingerprint property
    untouched, which `fingerprint` alone would miss.
    """

    href: str
    etag: str | None
    target_uid: str
    source_id: str
    source_uid: str
    recurrence_key_raw: str
    fingerprint: str
    vevent: object  # icalendar.cal.Event

    @property
    def instance_key_hint(self) -> str:
        return f"{self.source_id}|{self.source_uid}|{self.recurrence_key_raw}"


@dataclass(frozen=True)
class WriteResult:
    href: str
    etag: str | None
    status_code: int


@dataclass(frozen=True)
class SourceCapabilities:
    reachable: bool
    display_name: str | None = None


@dataclass
class PlanAction:
    change: ChangeType
    instance_key: str
    target_uid: str
    summary: str = ""
    ics_bytes: bytes | None = None
    fingerprint: str | None = None
    existing_href: str | None = None
    existing_etag: str | None = None


@dataclass
class ReconciliationPlan:
    creates: list[PlanAction] = field(default_factory=list)
    updates: list[PlanAction] = field(default_factory=list)
    deletes: list[PlanAction] = field(default_factory=list)
    unchanged: list[PlanAction] = field(default_factory=list)

    @property
    def total_writes(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.deletes)
