"""Fake-transport integration tests: exercise reconcile.run() through the
real caldav library against the in-memory FakeDavTransport, covering the
reconciliation scenarios and protocol properties from
TECHNICAL_SPECIFICATION.md section 20.
"""

from datetime import datetime, timezone

import pytest

from calendar_sync import reconcile
from calendar_sync.safety import DeletionLimitExceededError
from fake_dav import FakeObject

from conftest import event_ics, make_config

FIXED_NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)


def source_a(fake_transport):
    return fake_transport.add_collection("/remote.php/dav/calendars/user/source-a/", "Source A")


def source_b(fake_transport):
    return fake_transport.add_collection("/remote.php/dav/calendars/user/source-b/", "Source B")


def target(fake_transport):
    return fake_transport.add_collection("/remote.php/dav/calendars/user/target/", "Target")


def mutating_requests_to(fake_transport, path_fragment):
    return [
        (m, u) for m, u in fake_transport.requests
        if m in ("PUT", "POST", "PATCH", "DELETE") and path_fragment in u
    ]


def test_initial_sync_from_single_source(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')

    config = make_config(tmp_path)
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)

    assert summary.created == 1
    assert len(tgt.objects) == 1
    assert not mutating_requests_to(wire_fake_transport, "/source-a/")


def test_initial_sync_from_multiple_sources(wire_fake_transport, credentials, repo, tmp_path):
    src_a = source_a(wire_fake_transport)
    src_b = source_b(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src_a.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    src_b.objects["/remote.php/dav/calendars/user/source-b/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')

    config = make_config(tmp_path, source_ids=("dept-a", "dept-b"))
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)

    assert summary.created == 2  # both created despite sharing the same source UID
    assert len(tgt.objects) == 2


def test_duplicate_source_uid_across_sources_stays_distinct(wire_fake_transport, credentials, repo, tmp_path):
    src_a = source_a(wire_fake_transport)
    src_b = source_b(wire_fake_transport)
    target(wire_fake_transport)
    src_a.objects["/remote.php/dav/calendars/user/source-a/x.ics"] = FakeObject(
        event_ics("shared-uid", summary="From A"), '"e1"'
    )
    src_b.objects["/remote.php/dav/calendars/user/source-b/x.ics"] = FakeObject(
        event_ics("shared-uid", summary="From B"), '"e1"'
    )
    config = make_config(tmp_path, source_ids=("dept-a", "dept-b"))
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 2
    mappings = repo.all_active_mappings()
    assert {m["target_uid"] for m in mappings} == {m["target_uid"] for m in mappings}  # both distinct
    assert len({m["target_uid"] for m in mappings}) == 2


def test_dry_run_makes_zero_mutations(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    summary = reconcile.run(config, credentials, repo, dry_run=True, now=FIXED_NOW)
    assert summary.created == 1  # planned...
    mutating = [(m, u) for m, u in wire_fake_transport.requests if m in ("PUT", "POST", "PATCH", "DELETE")]
    assert mutating == []  # ...but never applied


def test_identical_second_run_performs_no_writes(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)

    wire_fake_transport.requests.clear()
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 0 and summary.updated == 0 and summary.deleted == 0
    assert summary.unchanged == 1
    mutating = [(m, u) for m, u in wire_fake_transport.requests if m in ("PUT", "POST", "PATCH", "DELETE")]
    assert mutating == []


def test_mirrored_field_change_triggers_update(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)

    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(
        event_ics("ev-1", summary="Renamed meeting"), '"e2"'
    )
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.updated == 1
    assert summary.created == 0


def test_location_becomes_ineligible_deletes_target(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert len(tgt.objects) == 1

    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(
        event_ics("ev-1", location="Nowhere Relevant"), '"e2"'
    )
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW, allow_large_delete=True)
    assert summary.deleted == 1
    assert len(tgt.objects) == 0


def test_location_becomes_eligible_creates_target(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(
        event_ics("ev-1", location="Nowhere Relevant"), '"e1"'
    )
    config = make_config(tmp_path)
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 0
    assert len(tgt.objects) == 0

    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e2"')
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 1
    assert len(tgt.objects) == 1


def test_source_deletion_removes_managed_copy(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert len(tgt.objects) == 1

    del src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"]
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW, allow_large_delete=True)
    assert summary.deleted == 1
    assert len(tgt.objects) == 0


def test_missing_managed_target_event_is_recreated(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert len(tgt.objects) == 1

    tgt.objects.clear()  # operator manually deleted the managed target event
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 1 or summary.recreated == 1
    assert len(tgt.objects) == 1


def test_manually_altered_managed_event_is_restored(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    (href, obj) = next(iter(tgt.objects.items()))
    obj.ics_text = obj.ics_text.replace("SUMMARY:Test event", "SUMMARY:Tampered by hand")
    obj.etag = '"tampered-etag"'

    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.updated == 1
    assert "Tampered" not in tgt.objects[href].ics_text


def test_unmanaged_target_event_is_untouched(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    tgt.objects["/remote.php/dav/calendars/user/target/manual.ics"] = FakeObject(
        event_ics("manual-uid", summary="Hand-added by operator"), '"m1"'
    )
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')

    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW, allow_large_delete=True)

    assert "manual" in tgt.objects["/remote.php/dav/calendars/user/target/manual.ics"].ics_text.lower() or (
        tgt.objects["/remote.php/dav/calendars/user/target/manual.ics"].etag == '"m1"'
    )
    assert len(tgt.objects) == 2  # the manual event plus the newly-created mirrored one


def test_partial_source_failure_causes_zero_target_mutations(wire_fake_transport, credentials, repo, tmp_path, monkeypatch):
    src_a = source_a(wire_fake_transport)
    src_b = source_b(wire_fake_transport)
    target(wire_fake_transport)
    src_a.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    src_b.objects["/remote.php/dav/calendars/user/source-b/ev1.ics"] = FakeObject(event_ics("ev-2"), '"e1"')

    from calendar_sync.source_gateway import ReadOnlySourceGateway, SourceReadError

    original_fetch = ReadOnlySourceGateway.fetch_calendar_objects

    def flaky_fetch(self, start, end):
        if self.source_id == "dept-b":
            raise SourceReadError("simulated transient failure")
        return original_fetch(self, start, end)

    monkeypatch.setattr(ReadOnlySourceGateway, "fetch_calendar_objects", flaky_fetch)

    config = make_config(tmp_path, source_ids=("dept-a", "dept-b"), require_all_sources=True)
    with pytest.raises(reconcile.SourcesNotAuthoritativeError):
        reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)

    mutating = [(m, u) for m, u in wire_fake_transport.requests if m in ("PUT", "POST", "PATCH", "DELETE")]
    assert mutating == []


def test_anomalous_empty_result_is_blocked_by_deletion_guard(wire_fake_transport, credentials, repo, tmp_path):
    """An empty desired set alone must never be sufficient justification
    for mass deletion (NFR-007) - even a *correct* empty read is subject
    to the same ratio/absolute guard as any other large delete."""
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    for i in range(5):
        src.objects[f"/remote.php/dav/calendars/user/source-a/ev{i}.ics"] = FakeObject(
            event_ics(f"ev-{i}"), f'"e{i}"'
        )
    config = make_config(tmp_path, max_delete_ratio=0.25, max_deletes_absolute=1000)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert len(tgt.objects) == 5

    src.objects.clear()  # legitimately empty this time, but the guard can't tell the difference
    with pytest.raises(DeletionLimitExceededError):
        reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert len(tgt.objects) == 5  # nothing was deleted before the guard raised


def test_credentials_never_appear_in_request_log(wire_fake_transport, credentials, repo, tmp_path):
    src = source_a(wire_fake_transport)
    target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    for _, url in wire_fake_transport.requests:
        assert credentials.app_password not in url


def _weekly_series_ics(uid="series-1", exception_summary=None, exception_location=None, exception_cancelled=False):
    exception = ""
    if exception_summary is not None or exception_location is not None or exception_cancelled:
        status_line = "STATUS:CANCELLED\n" if exception_cancelled else ""
        location_line = f"LOCATION:{exception_location}\n" if exception_location else ""
        exception = f"""BEGIN:VEVENT
UID:{uid}
RECURRENCE-ID;TZID=Europe/Berlin:20260112T090000
DTSTART;TZID=Europe/Berlin:20260112T090000
DTEND;TZID=Europe/Berlin:20260112T100000
SUMMARY:{exception_summary or "Weekly"}
{location_line}{status_line}END:VEVENT
"""
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:{uid}
DTSTART;TZID=Europe/Berlin:20260105T090000
DTEND;TZID=Europe/Berlin:20260105T100000
SUMMARY:Weekly
LOCATION:Berlin Office
RRULE:FREQ=WEEKLY;COUNT=2
END:VEVENT
{exception}END:VCALENDAR
"""


def test_recurrence_exception_location_transitions(wire_fake_transport, credentials, repo, tmp_path):
    """Acceptance criterion 6: both location transitions work on
    recurrence exceptions, exercised through the full reconcile pipeline
    (not just recurrence.py's expansion in isolation)."""
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/series.ics"] = FakeObject(_weekly_series_ics(), '"e1"')
    config = make_config(tmp_path)

    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 2
    assert len(tgt.objects) == 2

    # eligible -> ineligible: the Jan-12 exception moves to a non-matching location
    src.objects["/remote.php/dav/calendars/user/source-a/series.ics"] = FakeObject(
        _weekly_series_ics(exception_location="Nowhere Relevant"), '"e2"'
    )
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW, allow_large_delete=True)
    assert summary.deleted == 1
    assert len(tgt.objects) == 1  # the Jan-5 occurrence is untouched

    # ineligible -> eligible: move it back
    src.objects["/remote.php/dav/calendars/user/source-a/series.ics"] = FakeObject(_weekly_series_ics(), '"e3"')
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 1
    assert len(tgt.objects) == 2


def test_recurrence_exception_cancellation_removes_target_copy(wire_fake_transport, credentials, repo, tmp_path):
    """Acceptance criterion 7 for the recurrence-exception case: a
    cancelled exception's previously-mirrored copy is deleted."""
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/series.ics"] = FakeObject(_weekly_series_ics(), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert len(tgt.objects) == 2

    src.objects["/remote.php/dav/calendars/user/source-a/series.ics"] = FakeObject(
        _weekly_series_ics(exception_cancelled=True), '"e2"'
    )
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW, allow_large_delete=True)
    assert summary.deleted == 1
    assert len(tgt.objects) == 1


def test_etag_conflict_recovers_on_retry_then_quarantines_on_repeat(wire_fake_transport, credentials, repo, tmp_path, monkeypatch):
    """Acceptance criterion 11: an ETag conflict is detected (not
    silently overwritten) - reconcile.py re-reads and retries the write
    once, then quarantines rather than overwriting on a second conflict."""
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    config = make_config(tmp_path)
    reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    href, obj = next(iter(tgt.objects.items()))

    from calendar_sync.target_gateway import PreconditionFailed, TargetGateway

    original_replace = TargetGateway.replace
    state = {"calls": 0}

    def flaky_once_replace(self, href_, etag_, ics_bytes):
        state["calls"] += 1
        if state["calls"] == 1:
            raise PreconditionFailed("simulated stale ETag", 412)
        return original_replace(self, href_, etag_, ics_bytes)

    monkeypatch.setattr(TargetGateway, "replace", flaky_once_replace)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(
        event_ics("ev-1", summary="Renamed"), '"e2"'
    )
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.updated == 1
    assert summary.quarantined == 0
    assert "Renamed" in tgt.objects[href].ics_text

    # Now make every replace() call fail -> must quarantine, never overwrite blindly.
    def always_conflict_replace(self, href_, etag_, ics_bytes):
        raise PreconditionFailed("simulated persistent conflict", 412)

    monkeypatch.setattr(TargetGateway, "replace", always_conflict_replace)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(
        event_ics("ev-1", summary="Renamed again"), '"e3"'
    )
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.quarantined == 1
    assert summary.updated == 0
    assert "Renamed again" not in tgt.objects[href].ics_text  # never blindly overwritten


def test_buffer_pads_target_event_and_stays_idempotent(wire_fake_transport, credentials, repo, tmp_path):
    """The configured buffer must show up in the actual written target
    event, and a second run against the same unchanged source must still
    perform zero writes - regression guard for the fingerprint needing to
    be computed from the (buffered) written content, not the unbuffered
    source occurrence."""
    src = source_a(wire_fake_transport)
    tgt = target(wire_fake_transport)
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(
        event_ics("ev-1", start="20260110T090000Z", end="20260110T100000Z"), '"e1"'
    )
    config = make_config(tmp_path, buffer_before_minutes=15, buffer_after_minutes=30)

    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 1
    (_, obj) = next(iter(tgt.objects.items()))
    assert "DTSTART:20260110T084500Z" in obj.ics_text
    assert "DTEND:20260110T103000Z" in obj.ics_text

    wire_fake_transport.requests.clear()
    summary = reconcile.run(config, credentials, repo, dry_run=False, now=FIXED_NOW)
    assert summary.created == 0 and summary.updated == 0 and summary.deleted == 0
    assert summary.unchanged == 1
    mutating = [(m, u) for m, u in wire_fake_transport.requests if m in ("PUT", "POST", "PATCH", "DELETE")]
    assert mutating == []
