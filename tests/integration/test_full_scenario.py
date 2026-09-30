"""End-to-end scenario test against the fake DAV transport, covering the
combination explicitly requested for final validation:

- two source calendars, each with both matching- and non-matching-
  location events (single and recurring),
- only matching occurrences are mirrored, each padded by the configured
  before/after buffer,
- an unchanged second run writes nothing,
- deleting or changing a source event (single or one occurrence of a
  series) propagates to its target copy,
- deleting an entire source series removes all of its mirrored
  occurrences from the target.
"""

from datetime import datetime, timezone

from calendar_sync import reconcile
from fake_dav import FakeObject

from conftest import CanonicalLocation, event_ics, make_config

FIXED_NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)

LOCATIONS = (
    CanonicalLocation(canonical="Berlin Office", aliases=("Berlin Office",)),
    CanonicalLocation(canonical="Hamburg Office", aliases=("Hamburg Office",)),
)

BUFFER_BEFORE = 15
BUFFER_AFTER = 30


def weekly_series_ics(uid, *, location, start_date, exception=None):
    """A 2-occurrence weekly series (Europe/Berlin, 10:00-11:00 local on
    `start_date` and +7 days). `exception`, if given, is a dict with any
    of summary/location/cancelled overriding the second occurrence."""
    exception = exception or {}
    year, month, day = start_date
    from datetime import date, timedelta

    second = date(year, month, day) + timedelta(days=7)
    rid = f"{second.strftime('%Y%m%d')}T100000"

    override = ""
    if exception:
        status_line = "STATUS:CANCELLED\n" if exception.get("cancelled") else ""
        loc_line = f"LOCATION:{exception['location']}\n" if "location" in exception else f"LOCATION:{location}\n"
        override = f"""BEGIN:VEVENT
UID:{uid}
RECURRENCE-ID;TZID=Europe/Berlin:{rid}
DTSTART;TZID=Europe/Berlin:{rid}
DTEND;TZID=Europe/Berlin:{second.strftime('%Y%m%d')}T110000
SUMMARY:{exception.get('summary', 'Weekly (exception)')}
{loc_line}{status_line}END:VEVENT
"""
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:{uid}
DTSTART;TZID=Europe/Berlin:{year:04d}{month:02d}{day:02d}T100000
DTEND;TZID=Europe/Berlin:{year:04d}{month:02d}{day:02d}T110000
SUMMARY:Weekly
LOCATION:{location}
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
{override}END:VCALENDAR
"""


def test_two_sources_location_filter_buffer_and_propagation(wire_fake_transport, credentials, repo, tmp_path):
    fake = wire_fake_transport
    src_a = fake.add_collection("/remote.php/dav/calendars/user/source-a/", "Source A")
    src_b = fake.add_collection("/remote.php/dav/calendars/user/source-b/", "Source B")
    tgt = fake.add_collection("/remote.php/dav/calendars/user/target/", "Target")

    # -- Source A: one matching single, one non-matching single, one matching series --
    src_a.objects["/remote.php/dav/calendars/user/source-a/a1.ics"] = FakeObject(
        event_ics("single-a1", summary="A1 matches", location="Berlin Office",
                   start="20260109T090000Z", end="20260109T100000Z"),
        '"a1-e1"',
    )
    src_a.objects["/remote.php/dav/calendars/user/source-a/a2.ics"] = FakeObject(
        event_ics("single-a2", summary="A2 does not match", location="Nowhere Relevant",
                   start="20260107T090000Z", end="20260107T100000Z"),
        '"a2-e1"',
    )
    src_a.objects["/remote.php/dav/calendars/user/source-a/a3.ics"] = FakeObject(
        weekly_series_ics("series-a3", location="Berlin Office", start_date=(2026, 1, 5)),
        '"a3-e1"',
    )

    # -- Source B: one matching single, one non-matching single, one matching series --
    src_b.objects["/remote.php/dav/calendars/user/source-b/b1.ics"] = FakeObject(
        event_ics("single-b1", summary="B1 matches", location="Hamburg Office",
                   start="20260110T090000Z", end="20260110T100000Z"),
        '"b1-e1"',
    )
    src_b.objects["/remote.php/dav/calendars/user/source-b/b2.ics"] = FakeObject(
        event_ics("single-b2", summary="B2 does not match", location="Random Place",
                   start="20260108T090000Z", end="20260108T100000Z"),
        '"b2-e1"',
    )
    src_b.objects["/remote.php/dav/calendars/user/source-b/b3.ics"] = FakeObject(
        weekly_series_ics("series-b3", location="Hamburg Office", start_date=(2026, 1, 6)),
        '"b3-e1"',
    )

    config = make_config(
        tmp_path, source_ids=("dept-a", "dept-b"), locations=LOCATIONS,
        buffer_before_minutes=BUFFER_BEFORE, buffer_after_minutes=BUFFER_AFTER,
    )

    # === Run 1: initial sync ===
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    # 1 (A1) + 2 (series-a3 occurrences) + 1 (B1) + 2 (series-b3 occurrences) = 6
    assert summary.created == 6
    assert len(tgt.objects) == 6

    def target_texts():
        return [o.ics_text for o in tgt.objects.values()]

    texts = target_texts()
    assert any("A1 matches" in t for t in texts)
    assert not any("A2 does not match" in t for t in texts)
    assert not any("B2 does not match" in t for t in texts)
    assert sum(1 for t in texts if "X-CALMIRROR-SOURCE-UID:series-a3" in t) == 2
    assert sum(1 for t in texts if "X-CALMIRROR-SOURCE-UID:series-b3" in t) == 2

    # Buffer applied: A1 was 09:00-10:00Z -> target 08:45Z-10:30Z.
    a1_text = next(t for t in texts if "A1 matches" in t)
    assert "DTSTART:20260109T084500Z" in a1_text
    assert "DTEND:20260109T103000Z" in a1_text
    # series-a3 first occurrence was 10:00-11:00 Europe/Berlin -> 09:45-11:30 local.
    series_a3_texts = [t for t in texts if "X-CALMIRROR-SOURCE-UID:series-a3" in t]
    assert any("DTSTART;TZID=Europe/Berlin:20260105T094500" in t for t in series_a3_texts)
    assert any("DTEND;TZID=Europe/Berlin:20260105T113000" in t for t in series_a3_texts)

    # === Run 2: unchanged -> zero writes, nothing duplicated ===
    fake.requests.clear()
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 0 and summary.updated == 0 and summary.deleted == 0
    assert summary.unchanged == 6
    assert len(tgt.objects) == 6
    mutating = [(m, u) for m, u in fake.requests if m in ("PUT", "POST", "PATCH", "DELETE")]
    assert mutating == []

    # === Delete a single source event (A1) -> its target copy is removed ===
    del src_a.objects["/remote.php/dav/calendars/user/source-a/a1.ics"]
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.deleted == 1
    assert len(tgt.objects) == 5
    assert not any("A1 matches" in t for t in target_texts())

    # === Change one occurrence of a series (B3's second occurrence gets a
    # new summary) -> only that occurrence's target copy is updated ===
    src_b.objects["/remote.php/dav/calendars/user/source-b/b3.ics"] = FakeObject(
        weekly_series_ics(
            "series-b3", location="Hamburg Office", start_date=(2026, 1, 6),
            exception={"summary": "B3 second occurrence renamed"},
        ),
        '"b3-e2"',
    )
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.updated == 1
    assert summary.created == 0 and summary.deleted == 0
    assert len(tgt.objects) == 5
    texts = target_texts()
    assert any("B3 second occurrence renamed" in t for t in texts)
    series_b3_texts = [t for t in texts if "X-CALMIRROR-SOURCE-UID:series-b3" in t]
    assert len(series_b3_texts) == 2  # still both occurrences, one just renamed

    # === Delete the entire A3 series from the source -> both mirrored
    # occurrences disappear from the target ===
    del src_a.objects["/remote.php/dav/calendars/user/source-a/a3.ics"]
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.deleted == 2
    assert len(tgt.objects) == 3
    texts = target_texts()
    assert not any("X-CALMIRROR-SOURCE-UID:series-a3" in t for t in texts)
    # What remains: B1 (single) and both series-b3 occurrences.
    assert any("B1 matches" in t for t in texts)
    assert sum(1 for t in texts if "X-CALMIRROR-SOURCE-UID:series-b3" in t) == 2

    # Sources were only ever read, never mutated, across the whole scenario.
    source_mutations = [
        (m, u) for m, u in fake.requests
        if m in ("PUT", "POST", "PATCH", "DELETE") and ("/source-a/" in u or "/source-b/" in u)
    ]
    assert source_mutations == []
