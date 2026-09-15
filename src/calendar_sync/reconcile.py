"""Reconciliation engine: the phased algorithm from
TECHNICAL_SPECIFICATION.md section 16, tying every other module together.

No target mutation happens before every source that `safety.require_all_sources`
requires has been read authoritatively (Phase A/B), and every plan is
checked against the deletion/runtime guards before Phase D's first write.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .credentials import Credentials
from .ical import IcalParseError, extract_managed_metadata, parse_vevents
from .location import LocationMatcher
from .models import ChangeType, DesiredInstance, PlanAction, ReconciliationPlan, target_uid as compute_target_uid
from .recurrence import RecurrenceExpansionError, expand_source_object
from .safety import (
    DeletionLimitExceededError,
    RuntimeBudget,
    RuntimeBudgetExceededError,
    check_deletion_limits,
)
from .source_gateway import ReadOnlySourceGateway, SourceReadError, build_source_client
from .state import StateRepository
from .target_gateway import PreconditionFailed, TargetGateway, TargetWriteError, build_target_client
from .transform import build_target_event
from .transport import TargetContainmentGuard

log = logging.getLogger("calendar_sync.reconcile")


class PreflightError(Exception):
    """The target calendar (or, in strict preflight mode, a source) could
    not be reached/authenticated. No target mutation is attempted."""


class SourcesNotAuthoritativeError(Exception):
    """One or more required sources could not be read authoritatively.
    Callers must treat this as "zero target mutations happened", never as
    "those sources' events are gone" (NFR-012)."""


@dataclass
class RunSummary:
    run_id: str
    dry_run: bool
    window_start: datetime
    window_end: datetime
    status: str = "success"
    created: int = 0
    updated: int = 0
    recreated: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped: int = 0
    quarantined: int = 0
    failed: int = 0
    error_summary: str | None = None
    source_errors: dict = field(default_factory=dict)


def compute_window(config: Config, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """One immutable half-open [window_start, window_end) interval,
    calculated once per run in the configured timezone and converted to
    UTC for DAV query boundaries (spec section 11)."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(config.window.timezone)
    local_now = now.astimezone(tz)
    start_local = local_now - timedelta(days=config.window.lookback_days)
    end_local = local_now + timedelta(days=config.window.lookahead_days)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _build_guard(config: Config) -> TargetContainmentGuard:
    return TargetContainmentGuard(
        canonical_target=config.canonical_target,
        canonical_sources=config.canonical_sources,
        allow_insecure_for_testing=config.nextcloud.allow_insecure_urls_for_testing,
    )


def preflight(config: Config, credentials: Credentials) -> dict[str, bool]:
    """Verify every enabled source and the target collection are reachable
    and authenticated, without issuing any mutating request."""
    results: dict[str, bool] = {}
    source_client = build_source_client(config.nextcloud, credentials)
    for source in config.sources:
        if not source.enabled:
            continue
        gateway = ReadOnlySourceGateway(source, source_client)
        results[source.id] = gateway.preflight().reachable

    guard = _build_guard(config)
    target_client = build_target_client(config.nextcloud, credentials, guard=guard)
    target_gateway = TargetGateway(config.target, target_client, credentials)
    results["__target__"] = target_gateway.preflight()
    return results


def _read_sources(
    config: Config,
    credentials: Credentials,
    window_start: datetime,
    window_end: datetime,
) -> tuple[dict[str, DesiredInstance], dict[str, str]]:
    """Phase B: read every enabled source, expand and filter its
    occurrences, and transform eligible ones into desired instances.
    Returns (desired_by_instance_key, read_errors_by_source_id)."""
    source_client = build_source_client(config.nextcloud, credentials)
    matcher = LocationMatcher(config.location_filter)
    desired: dict[str, DesiredInstance] = {}
    errors: dict[str, str] = {}

    for source in config.sources:
        if not source.enabled:
            continue
        gateway = ReadOnlySourceGateway(source, source_client)
        try:
            resources = gateway.fetch_calendar_objects(window_start, window_end)
            for resource in resources:
                vevents = parse_vevents(resource.ics_text)
                occurrences = expand_source_object(
                    source.id,
                    resource.href,
                    resource.etag,
                    vevents,
                    window_start,
                    window_end,
                    max_expansion_count=config.safety.max_expansion_count,
                )
                for occurrence in occurrences:
                    if occurrence.cancelled:
                        continue
                    canonical_location = matcher.match(occurrence.location)
                    if canonical_location is None:
                        continue
                    target_uid_value = compute_target_uid(occurrence.instance_key)
                    desired_instance = build_target_event(
                        occurrence, canonical_location, config.mirroring, target_uid_value
                    )
                    desired[desired_instance.instance_key] = desired_instance
        except (SourceReadError, IcalParseError, RecurrenceExpansionError) as exc:
            log.error("Source '%s' could not be read authoritatively: %s", source.id, exc)
            errors[source.id] = str(exc)

    return desired, errors


def _build_plan(
    desired: dict[str, DesiredInstance],
    active_mappings: dict[str, "sqlite3.Row"],
    target_gateway: TargetGateway,
    window_start: datetime,
    window_end: datetime,
    failed_source_ids: set[str],
) -> ReconciliationPlan:
    """Phase C: deterministic plan, sorted by instance key within each
    group, creates before updates before deletes."""
    managed = target_gateway.list_managed(window_start, window_end)
    managed_by_uid = {m.target_uid: m for m in managed}
    managed_by_key = {m.instance_key_hint: m for m in managed}

    plan = ReconciliationPlan()
    for instance_key in sorted(desired):
        desired_instance = desired[instance_key]
        managed_entry = managed_by_uid.get(desired_instance.target_uid)
        mapping = active_mappings.get(instance_key)

        if managed_entry is None:
            change = ChangeType.RECREATE if mapping is not None else ChangeType.CREATE
            plan.creates.append(
                PlanAction(
                    change=change,
                    instance_key=instance_key,
                    target_uid=desired_instance.target_uid,
                    summary=desired_instance.summary,
                    ics_bytes=desired_instance.ics_bytes,
                    fingerprint=desired_instance.fingerprint,
                )
            )
        elif managed_entry.fingerprint == desired_instance.fingerprint:
            plan.unchanged.append(
                PlanAction(
                    change=ChangeType.UNCHANGED,
                    instance_key=instance_key,
                    target_uid=desired_instance.target_uid,
                    fingerprint=desired_instance.fingerprint,
                    existing_href=managed_entry.href,
                    existing_etag=managed_entry.etag,
                )
            )
        else:
            plan.updates.append(
                PlanAction(
                    change=ChangeType.UPDATE,
                    instance_key=instance_key,
                    target_uid=desired_instance.target_uid,
                    summary=desired_instance.summary,
                    ics_bytes=desired_instance.ics_bytes,
                    fingerprint=desired_instance.fingerprint,
                    existing_href=managed_entry.href,
                    existing_etag=managed_entry.etag,
                )
            )

    # Delete candidates: every previously-known instance (mapping row or a
    # currently-managed target object, in case the DB lost a row) that is
    # no longer desired - but never one whose source failed to read this
    # run, since we don't authoritatively know whether it's still eligible.
    candidate_keys = set(active_mappings) | set(managed_by_key)
    for instance_key in sorted(candidate_keys):
        if instance_key in desired:
            continue
        mapping = active_mappings.get(instance_key)
        managed_entry = managed_by_key.get(instance_key)
        source_id = mapping["source_id"] if mapping is not None else managed_entry.source_id
        if source_id in failed_source_ids:
            continue
        href = managed_entry.href if managed_entry is not None else mapping["target_href"]
        etag = managed_entry.etag if managed_entry is not None else mapping["target_etag"]
        target_uid_value = managed_entry.target_uid if managed_entry is not None else mapping["target_uid"]
        plan.deletes.append(
            PlanAction(
                change=ChangeType.DELETE,
                instance_key=instance_key,
                target_uid=target_uid_value,
                existing_href=href,
                existing_etag=etag,
            )
        )
    return plan


def _record_instance(repo: StateRepository, action: PlanAction, desired: dict[str, DesiredInstance], *, href: str, etag: str | None, run_id: str) -> None:
    desired_instance = desired[action.instance_key]
    repo.record_write(
        instance_key=action.instance_key,
        source_id=desired_instance.source_id,
        source_href=desired_instance.source_href,
        source_uid=desired_instance.source_uid,
        recurrence_key=desired_instance.recurrence_key.canonical(),
        source_etag=desired_instance.source_etag,
        target_href=href,
        target_uid=action.target_uid,
        target_etag=etag,
        fingerprint=action.fingerprint or desired_instance.fingerprint,
        start_utc=desired_instance.start.astimezone(timezone.utc).isoformat()
        if hasattr(desired_instance.start, "astimezone")
        else desired_instance.start.isoformat(),
        end_utc=desired_instance.end.astimezone(timezone.utc).isoformat()
        if hasattr(desired_instance.end, "astimezone")
        else desired_instance.end.isoformat(),
        run_id=run_id,
    )


def _apply_create(
    target_gateway: TargetGateway, repo: StateRepository, action: PlanAction, desired, run_id: str
) -> ChangeType:
    try:
        result = target_gateway.create(action.target_uid, action.ics_bytes)
        _record_instance(repo, action, desired, href=result.href, etag=result.etag, run_id=run_id)
        return action.change  # CREATE or RECREATE
    except PreconditionFailed:
        existing = target_gateway.get_object_by_uid(action.target_uid)
        if existing is None:
            # Raced with something else; the slot is free again.
            result = target_gateway.create(action.target_uid, action.ics_bytes)
            _record_instance(repo, action, desired, href=result.href, etag=result.etag, run_id=run_id)
            return action.change
        managed = extract_managed_metadata(existing.href, existing.etag, parse_vevents(existing.ics_text))
        if managed is not None and managed.instance_key_hint == action.instance_key:
            # It's our own object under a stale ETag view - treat as an update.
            result = target_gateway.replace(existing.href, existing.etag, action.ics_bytes)
            _record_instance(repo, action, desired, href=result.href, etag=result.etag, run_id=run_id)
            return ChangeType.UPDATE
        log.warning(
            "Quarantining %s: an unowned object already occupies its deterministic UID.",
            action.target_uid,
        )
        repo.record_quarantined(action.instance_key)
        return ChangeType.QUARANTINE


def _apply_update(
    target_gateway: TargetGateway, repo: StateRepository, action: PlanAction, desired, run_id: str
) -> ChangeType:
    try:
        result = target_gateway.replace(action.existing_href, action.existing_etag, action.ics_bytes)
        _record_instance(repo, action, desired, href=result.href, etag=result.etag, run_id=run_id)
        return ChangeType.UPDATE
    except PreconditionFailed:
        existing = target_gateway.get_object(action.existing_href)
        if existing is None:
            result = target_gateway.create(action.target_uid, action.ics_bytes)
            _record_instance(repo, action, desired, href=result.href, etag=result.etag, run_id=run_id)
            return ChangeType.RECREATE
        try:
            result = target_gateway.replace(existing.href, existing.etag, action.ics_bytes)
            _record_instance(repo, action, desired, href=result.href, etag=result.etag, run_id=run_id)
            return ChangeType.UPDATE
        except PreconditionFailed:
            log.warning("Quarantining %s after a repeated update conflict.", action.target_uid)
            repo.record_quarantined(action.instance_key)
            return ChangeType.QUARANTINE


def _apply_delete(target_gateway: TargetGateway, repo: StateRepository, action: PlanAction) -> ChangeType:
    try:
        target_gateway.delete(action.existing_href, action.existing_etag)
        repo.record_deleted(action.instance_key)
        return ChangeType.DELETE
    except PreconditionFailed:
        existing = target_gateway.get_object(action.existing_href)
        if existing is None:
            repo.record_deleted(action.instance_key)
            return ChangeType.DELETE
        try:
            target_gateway.delete(existing.href, existing.etag)
            repo.record_deleted(action.instance_key)
            return ChangeType.DELETE
        except PreconditionFailed:
            log.warning("Quarantining %s after a repeated delete conflict.", action.target_uid)
            repo.record_quarantined(action.instance_key)
            return ChangeType.QUARANTINE


def run(
    config: Config,
    credentials: Credentials,
    repo: StateRepository,
    *,
    dry_run: bool,
    allow_large_delete: bool = False,
    now: datetime | None = None,
) -> RunSummary:
    """Execute one full reconciliation run (Phases A-E)."""
    run_id = str(uuid.uuid4())
    now = now or datetime.now(timezone.utc)
    window_start, window_end = compute_window(config, now=now)
    repo.start_run(
        run_id,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        dry_run=dry_run,
    )
    summary = RunSummary(run_id=run_id, dry_run=dry_run, window_start=window_start, window_end=window_end)
    budget = RuntimeBudget(config.safety.max_runtime_seconds)

    guard = _build_guard(config)
    target_client = build_target_client(config.nextcloud, credentials, guard=guard)
    target_gateway = TargetGateway(config.target, target_client, credentials)

    if not target_gateway.preflight():
        summary.status = "failed"
        summary.error_summary = "Target calendar could not be reached/authenticated."
        repo.finish_run(run_id, status="failed", error_summary=summary.error_summary)
        raise PreflightError(summary.error_summary)

    # Phase B
    desired, read_errors = _read_sources(config, credentials, window_start, window_end)
    summary.source_errors = read_errors
    for source in config.sources:
        if not source.enabled:
            continue
        if source.id in read_errors:
            repo.record_source_error(source.id, source.calendar_url, read_errors[source.id])
        else:
            repo.record_source_success(source.id, source.calendar_url)

    failed_source_ids = set(read_errors)
    if read_errors and config.safety.require_all_sources:
        summary.status = "aborted"
        summary.error_summary = "; ".join(f"{k}: {v}" for k, v in read_errors.items())
        repo.finish_run(run_id, status="aborted", error_summary=summary.error_summary)
        raise SourcesNotAuthoritativeError(summary.error_summary)

    # Phase C
    active_mappings = {row["instance_key"]: row for row in repo.all_active_mappings()}
    plan = _build_plan(desired, active_mappings, target_gateway, window_start, window_end, failed_source_ids)

    try:
        check_deletion_limits(
            len(plan.deletes),
            baseline_active_mappings=len(active_mappings),
            safety=config.safety,
            allow_large_delete=allow_large_delete,
        )
    except DeletionLimitExceededError as exc:
        summary.status = "aborted"
        summary.error_summary = str(exc)
        repo.finish_run(run_id, status="aborted", error_summary=summary.error_summary)
        raise

    summary.unchanged = len(plan.unchanged)
    summary.skipped = len(failed_source_ids)

    if dry_run:
        summary.created = sum(1 for a in plan.creates if a.change == ChangeType.CREATE)
        summary.recreated = sum(1 for a in plan.creates if a.change == ChangeType.RECREATE)
        summary.updated = len(plan.updates)
        summary.deleted = len(plan.deletes)
        repo.finish_run(
            run_id,
            status="success",
            created=summary.created,
            updated=summary.updated + summary.recreated,
            deleted=summary.deleted,
            unchanged=summary.unchanged,
        )
        log.info(
            "[dry-run] plan: %d create, %d recreate, %d update, %d delete, %d unchanged",
            summary.created, summary.recreated, summary.updated, summary.deleted, summary.unchanged,
        )
        return summary

    # Phase D: creates, then updates, then deletes.
    for action in plan.creates:
        budget.ensure_not_expired()
        outcome = _apply_create(target_gateway, repo, action, desired, run_id)
        _tally(summary, outcome)

    for action in plan.updates:
        budget.ensure_not_expired()
        outcome = _apply_update(target_gateway, repo, action, desired, run_id)
        _tally(summary, outcome)

    for action in plan.deletes:
        budget.ensure_not_expired()
        outcome = _apply_delete(target_gateway, repo, action)
        _tally(summary, outcome)

    repo.finish_run(
        run_id,
        status="success",
        created=summary.created,
        updated=summary.updated + summary.recreated,
        deleted=summary.deleted,
        unchanged=summary.unchanged,
        skipped=summary.skipped,
        failed=summary.failed,
    )
    return summary


def _tally(summary: RunSummary, outcome: ChangeType) -> None:
    if outcome == ChangeType.CREATE:
        summary.created += 1
    elif outcome == ChangeType.RECREATE:
        summary.recreated += 1
    elif outcome == ChangeType.UPDATE:
        summary.updated += 1
    elif outcome == ChangeType.DELETE:
        summary.deleted += 1
    elif outcome == ChangeType.QUARANTINE:
        summary.quarantined += 1
    elif outcome == ChangeType.FAILED:
        summary.failed += 1
