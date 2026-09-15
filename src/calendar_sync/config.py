"""JSON configuration loading and validation.

JSON is used instead of TOML so Python 3.10 (which lacks `tomllib`) needs
no extra dependency. All paths in the config are project-relative and are
validated through `paths.resolve_project_path` before use; all calendar
URLs are validated through `urlsafety.canonicalize_url` and checked for
source/target separation before any network or filesystem write path
opens - see acceptance criteria in TECHNICAL_SPECIFICATION.md section 21.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PathEscapesProjectRootError, resolve_project_path
from .textnorm import normalize_location
from .urlsafety import CanonicalUrl, UrlSafetyError, canonicalize_url

log = logging.getLogger("calendar_sync.config")

SUPPORTED_MATCH_MODES = ("normalized_exact",)
SUPPORTED_OUTSIDE_WINDOW_POLICIES = ("retain",)

# UID/DTSTAMP/etc. are always excluded by transform.py's own allowlist
# regardless of strip_fields, and DTSTART is required for the transform to
# even produce a valid occurrence - listing any of these has no effect,
# so warn rather than silently ignoring it (carried over from the
# predecessor project's PROTECTED_FIELDS behavior).
PROTECTED_STRIP_FIELDS = frozenset({"UID", "DTSTAMP", "DTSTART", "RECURRENCE-ID"})


class ConfigError(ValueError):
    """Configuration is missing, malformed, or fails validation.

    Messages are always safe to log: they never include secret values,
    because secrets never enter the JSON config in the first place.
    """


@dataclass(frozen=True)
class NextcloudConfig:
    base_url: str
    credentials_file: str
    verify_tls: bool
    connect_timeout_seconds: float
    read_timeout_seconds: float
    user_agent: str
    allow_insecure_urls_for_testing: bool


@dataclass(frozen=True)
class SourceConfig:
    id: str
    calendar_url: str
    enabled: bool


@dataclass(frozen=True)
class TargetConfig:
    calendar_url: str


@dataclass(frozen=True)
class CanonicalLocation:
    canonical: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class LocationFilterConfig:
    match_mode: str
    case_sensitive: bool
    locations: tuple[CanonicalLocation, ...]


@dataclass(frozen=True)
class WindowConfig:
    timezone: str
    lookback_days: int
    lookahead_days: int
    outside_window_policy: str


@dataclass(frozen=True)
class MirroringConfig:
    copy_description: bool
    copy_url: bool
    copy_categories: bool
    copy_alarms: bool
    copy_attendees: bool
    copy_organizer: bool
    strip_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorageConfig:
    database: str
    log_file: str
    lock_file: str
    backup_directory: str
    backup_retention: int = 10


@dataclass(frozen=True)
class SafetyConfig:
    dry_run_default: bool
    max_deletes_absolute: int
    max_delete_ratio: float
    max_runtime_seconds: int
    require_all_sources: bool
    max_expansion_count: int = 5000


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    max_bytes: int = 1_000_000
    backup_count: int = 5


@dataclass(frozen=True)
class Config:
    nextcloud: NextcloudConfig
    sources: tuple[SourceConfig, ...]
    target: TargetConfig
    location_filter: LocationFilterConfig
    window: WindowConfig
    mirroring: MirroringConfig
    storage: StorageConfig
    safety: SafetyConfig
    logging: LoggingConfig
    project_root: Path = field(compare=False)

    # Populated by validation for reuse by callers (transport guard, gateways)
    # so canonicalization never has to run twice with a chance to disagree.
    canonical_base: CanonicalUrl = field(compare=False)
    canonical_target: CanonicalUrl = field(compare=False)
    canonical_sources: tuple[CanonicalUrl, ...] = field(compare=False)

    def resolved_path(self, relative: str) -> Path:
        return resolve_project_path(relative, root=self.project_root)


def _require(data: dict, key: str, path: str) -> object:
    if key not in data:
        raise ConfigError(f"Missing required config key: {path}.{key}")
    return data[key]


def load_config(config_path: Path, *, project_root: Path) -> Config:
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config file must contain a JSON object at the top level.")

    nc_raw = _require(raw, "nextcloud", "$")
    nc = NextcloudConfig(
        base_url=_require(nc_raw, "base_url", "nextcloud"),
        credentials_file=nc_raw.get("credentials_file", "secrets/nextcloud.env"),
        verify_tls=bool(nc_raw.get("verify_tls", True)),
        connect_timeout_seconds=float(nc_raw.get("connect_timeout_seconds", 10)),
        read_timeout_seconds=float(nc_raw.get("read_timeout_seconds", 45)),
        # Some hosting providers front Nextcloud with a firewall/WAF that
        # blocks the default User-Agent sent by common Python HTTP
        # libraries (requests/niquests) while allowing e.g. curl through
        # unaffected - observed on IONOS-hosted Nextcloud behind STRATO
        # shared webspace. Overriding this header is the documented
        # workaround; see transport.py.
        user_agent=nc_raw.get("user_agent", "calendar-sync/1.0"),
        allow_insecure_urls_for_testing=bool(nc_raw.get("allow_insecure_urls_for_testing", False)),
    )
    if nc.connect_timeout_seconds <= 0 or nc.read_timeout_seconds <= 0:
        raise ConfigError("nextcloud.connect_timeout_seconds and read_timeout_seconds must be > 0.")

    sources_raw = _require(raw, "sources", "$")
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ConfigError("sources must be a non-empty list.")
    sources = []
    seen_ids = set()
    for i, s in enumerate(sources_raw):
        source_id = str(_require(s, "id", f"sources[{i}]"))
        if not source_id.strip():
            raise ConfigError(f"sources[{i}].id must not be empty.")
        if source_id in seen_ids:
            raise ConfigError(f"Duplicate sources[].id: {source_id!r}")
        seen_ids.add(source_id)
        sources.append(
            SourceConfig(
                id=source_id,
                calendar_url=str(_require(s, "calendar_url", f"sources[{i}]")),
                enabled=bool(s.get("enabled", True)),
            )
        )
    if not any(s.enabled for s in sources):
        raise ConfigError("At least one entry in sources must have enabled=true.")

    target_raw = _require(raw, "target", "$")
    target = TargetConfig(calendar_url=str(_require(target_raw, "calendar_url", "target")))

    lf_raw = _require(raw, "location_filter", "$")
    match_mode = lf_raw.get("match_mode", "normalized_exact")
    if match_mode not in SUPPORTED_MATCH_MODES:
        raise ConfigError(
            f"location_filter.match_mode must be one of {SUPPORTED_MATCH_MODES}, got {match_mode!r}."
        )
    case_sensitive = bool(lf_raw.get("case_sensitive", False))
    locations_raw = lf_raw.get("locations") or []
    if not locations_raw:
        raise ConfigError("location_filter.locations must contain at least one entry.")
    locations = []
    alias_to_canonical: dict[str, str] = {}
    for i, loc in enumerate(locations_raw):
        canonical = str(_require(loc, "canonical", f"location_filter.locations[{i}]")).strip()
        if not canonical:
            raise ConfigError(f"location_filter.locations[{i}].canonical must not be empty.")
        aliases_raw = loc.get("aliases") or [canonical]
        aliases = tuple(str(a) for a in aliases_raw if str(a).strip())
        if not aliases:
            raise ConfigError(f"location_filter.locations[{i}] has no non-empty aliases.")
        for alias in aliases:
            normalized = normalize_location(alias, case_sensitive=case_sensitive)
            existing = alias_to_canonical.get(normalized)
            if existing is not None and existing != canonical:
                raise ConfigError(
                    f"Alias {alias!r} normalizes to a value already mapped to "
                    f"canonical location {existing!r}, cannot also map to {canonical!r}."
                )
            alias_to_canonical[normalized] = canonical
        locations.append(CanonicalLocation(canonical=canonical, aliases=aliases))
    location_filter = LocationFilterConfig(
        match_mode=match_mode, case_sensitive=case_sensitive, locations=tuple(locations)
    )

    window_raw = _require(raw, "window", "$")
    outside_policy = window_raw.get("outside_window_policy", "retain")
    if outside_policy not in SUPPORTED_OUTSIDE_WINDOW_POLICIES:
        raise ConfigError(
            f"window.outside_window_policy must be one of {SUPPORTED_OUTSIDE_WINDOW_POLICIES}, "
            f"got {outside_policy!r}."
        )
    lookback_days = int(window_raw.get("lookback_days", 7))
    lookahead_days = int(window_raw.get("lookahead_days", 180))
    if lookback_days < 0 or lookahead_days < 0:
        raise ConfigError("window.lookback_days and lookahead_days must be >= 0.")
    if lookback_days + lookahead_days <= 0:
        raise ConfigError("window.lookback_days + lookahead_days must be > 0.")
    window = WindowConfig(
        timezone=str(window_raw.get("timezone", "Europe/Berlin")),
        lookback_days=lookback_days,
        lookahead_days=lookahead_days,
        outside_window_policy=outside_policy,
    )

    mirroring_raw = raw.get("mirroring") or {}
    strip_fields = {str(f).upper() for f in (mirroring_raw.get("strip_fields") or [])}
    ignored = strip_fields & PROTECTED_STRIP_FIELDS
    if ignored:
        log.warning("Ignoring mirroring.strip_fields entries that cannot be removed: %s", sorted(ignored))
    strip_fields -= PROTECTED_STRIP_FIELDS
    mirroring = MirroringConfig(
        copy_description=bool(mirroring_raw.get("copy_description", True)),
        copy_url=bool(mirroring_raw.get("copy_url", True)),
        copy_categories=bool(mirroring_raw.get("copy_categories", True)),
        copy_alarms=bool(mirroring_raw.get("copy_alarms", False)),
        copy_attendees=bool(mirroring_raw.get("copy_attendees", False)),
        copy_organizer=bool(mirroring_raw.get("copy_organizer", False)),
        strip_fields=tuple(sorted(strip_fields)),
    )

    storage_raw = raw.get("storage") or {}
    storage = StorageConfig(
        database=str(storage_raw.get("database", "data/sync.sqlite3")),
        log_file=str(storage_raw.get("log_file", "logs/sync.log")),
        lock_file=str(storage_raw.get("lock_file", "run/calendar-sync.lock")),
        backup_directory=str(storage_raw.get("backup_directory", "data/backups")),
        backup_retention=int(storage_raw.get("backup_retention", 10)),
    )
    if storage.backup_retention < 1:
        raise ConfigError("storage.backup_retention must be >= 1.")
    for label, value in (
        ("storage.database", storage.database),
        ("storage.log_file", storage.log_file),
        ("storage.lock_file", storage.lock_file),
        ("storage.backup_directory", storage.backup_directory),
        ("nextcloud.credentials_file", nc.credentials_file),
    ):
        try:
            resolve_project_path(value, root=project_root)
        except PathEscapesProjectRootError as exc:
            raise ConfigError(f"{label} escapes the project root: {exc}") from exc

    safety_raw = raw.get("safety") or {}
    safety = SafetyConfig(
        dry_run_default=bool(safety_raw.get("dry_run_default", True)),
        max_deletes_absolute=int(safety_raw.get("max_deletes_absolute", 50)),
        max_delete_ratio=float(safety_raw.get("max_delete_ratio", 0.25)),
        max_runtime_seconds=int(safety_raw.get("max_runtime_seconds", 720)),
        require_all_sources=bool(safety_raw.get("require_all_sources", True)),
        max_expansion_count=int(safety_raw.get("max_expansion_count", 5000)),
    )
    if safety.max_deletes_absolute < 0:
        raise ConfigError("safety.max_deletes_absolute must be >= 0.")
    if not (0.0 <= safety.max_delete_ratio <= 1.0):
        raise ConfigError("safety.max_delete_ratio must be between 0.0 and 1.0.")
    if safety.max_runtime_seconds <= 0:
        raise ConfigError("safety.max_runtime_seconds must be > 0.")
    if safety.max_expansion_count <= 0:
        raise ConfigError("safety.max_expansion_count must be > 0.")

    logging_raw = raw.get("logging") or {}
    logging_cfg = LoggingConfig(
        level=str(logging_raw.get("level", "INFO")).upper(),
        max_bytes=int(logging_raw.get("max_bytes", 1_000_000)),
        backup_count=int(logging_raw.get("backup_count", 5)),
    )

    allow_insecure = nc.allow_insecure_urls_for_testing
    try:
        canonical_base = canonicalize_url(nc.base_url, allow_insecure=allow_insecure)
        canonical_target = canonicalize_url(target.calendar_url, allow_insecure=allow_insecure)
        canonical_sources = tuple(
            canonicalize_url(s.calendar_url, allow_insecure=allow_insecure) for s in sources
        )
    except UrlSafetyError as exc:
        raise ConfigError(str(exc)) from exc

    if not canonical_target.is_within(canonical_base):
        raise ConfigError(
            "target.calendar_url must share the configured Nextcloud origin and DAV hierarchy."
        )
    for s, canonical_source in zip(sources, canonical_sources):
        if not canonical_source.is_within(canonical_base):
            raise ConfigError(
                f"sources[{s.id!r}].calendar_url must share the configured Nextcloud "
                "origin and DAV hierarchy."
            )

    for s, canonical_source in zip(sources, canonical_sources):
        if canonical_source.path == canonical_target.path and canonical_source.origin == canonical_target.origin:
            raise ConfigError(
                f"target.calendar_url must not equal sources[{s.id!r}].calendar_url."
            )
        if canonical_source.is_within(canonical_target) or canonical_target.is_within(canonical_source):
            raise ConfigError(
                f"target.calendar_url must not overlap sources[{s.id!r}].calendar_url "
                "(one collection contains the other)."
            )

    return Config(
        nextcloud=nc,
        sources=tuple(sources),
        target=target,
        location_filter=location_filter,
        window=window,
        mirroring=mirroring,
        storage=storage,
        safety=safety,
        logging=logging_cfg,
        project_root=project_root,
        canonical_base=canonical_base,
        canonical_target=canonical_target,
        canonical_sources=canonical_sources,
    )
