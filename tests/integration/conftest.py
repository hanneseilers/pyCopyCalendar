import caldav
import pytest

from calendar_sync import reconcile, source_gateway, target_gateway
from calendar_sync.config import (
    CanonicalLocation,
    Config,
    LocationFilterConfig,
    LoggingConfig,
    MirroringConfig,
    NextcloudConfig,
    SafetyConfig,
    SourceConfig,
    StorageConfig,
    TargetConfig,
    WindowConfig,
)
from calendar_sync.credentials import Credentials
from calendar_sync.state import StateRepository, open_database
from calendar_sync.transport import CalendarSyncSession
from calendar_sync.urlsafety import canonicalize_url
from fake_dav import FakeDavTransport

BASE = "https://cloud.example.invalid/remote.php/dav/"
SOURCE_A_URL = "https://cloud.example.invalid/remote.php/dav/calendars/user/source-a/"
SOURCE_B_URL = "https://cloud.example.invalid/remote.php/dav/calendars/user/source-b/"
TARGET_URL = "https://cloud.example.invalid/remote.php/dav/calendars/user/target/"


@pytest.fixture
def fake_dav():
    return FakeDavTransport()


@pytest.fixture
def wire_fake_transport(monkeypatch, fake_dav):
    """Replace build_source_client/build_target_client everywhere
    reconcile.py imported them, so every gateway built during a test talks
    to the in-memory fake server instead of attempting real sockets."""

    def fake_build_source_client(nc, credentials):
        client = caldav.DAVClient(url=nc.base_url, username=credentials.username, password=credentials.app_password)
        client.session = CalendarSyncSession(fake_dav, allow_mutations=False)
        return client

    def fake_build_target_client(nc, credentials, *, guard):
        client = caldav.DAVClient(url=nc.base_url, username=credentials.username, password=credentials.app_password)
        client.session = CalendarSyncSession(fake_dav, allow_mutations=True, guard=guard)
        return client

    monkeypatch.setattr(source_gateway, "build_source_client", fake_build_source_client)
    monkeypatch.setattr(target_gateway, "build_target_client", fake_build_target_client)
    monkeypatch.setattr(reconcile, "build_source_client", fake_build_source_client)
    monkeypatch.setattr(reconcile, "build_target_client", fake_build_target_client)
    return fake_dav


@pytest.fixture
def credentials():
    return Credentials(username="testuser", app_password="s3cr3t-app-password")


def make_config(tmp_path, *, source_ids=("dept-a",), require_all_sources=True,
                 max_delete_ratio=1.0, max_deletes_absolute=1000, max_runtime_seconds=60,
                 locations=None):
    urls = {"dept-a": SOURCE_A_URL, "dept-b": SOURCE_B_URL}
    sources = tuple(SourceConfig(id=sid, calendar_url=urls[sid], enabled=True) for sid in source_ids)
    locations = locations or (CanonicalLocation(canonical="Berlin Office", aliases=("Berlin Office",)),)

    nc = NextcloudConfig(
        base_url=BASE, credentials_file="secrets/nextcloud.env", verify_tls=True,
        connect_timeout_seconds=5, read_timeout_seconds=5, user_agent="test/1.0",
        allow_insecure_urls_for_testing=False,
    )
    target = TargetConfig(calendar_url=TARGET_URL)
    lf = LocationFilterConfig(match_mode="normalized_exact", case_sensitive=False, locations=locations)
    window = WindowConfig(timezone="Europe/Berlin", lookback_days=7, lookahead_days=180, outside_window_policy="retain")
    mirroring = MirroringConfig(
        copy_description=True, copy_url=True, copy_categories=True,
        copy_alarms=False, copy_attendees=False, copy_organizer=False,
    )
    storage = StorageConfig(
        database="data/sync.sqlite3", log_file="logs/sync.log",
        lock_file="run/calendar-sync.lock", backup_directory="data/backups",
    )
    safety = SafetyConfig(
        dry_run_default=True, max_deletes_absolute=max_deletes_absolute, max_delete_ratio=max_delete_ratio,
        max_runtime_seconds=max_runtime_seconds, require_all_sources=require_all_sources,
    )
    logging_cfg = LoggingConfig()

    return Config(
        nextcloud=nc, sources=sources, target=target, location_filter=lf, window=window,
        mirroring=mirroring, storage=storage, safety=safety, logging=logging_cfg,
        project_root=tmp_path,
        canonical_base=canonicalize_url(BASE), canonical_target=canonicalize_url(TARGET_URL),
        canonical_sources=tuple(canonicalize_url(urls[sid]) for sid in source_ids),
    )


@pytest.fixture
def repo(tmp_path):
    conn = open_database(tmp_path / "data/sync.sqlite3", backup_dir=tmp_path / "data/backups")
    yield StateRepository(conn)
    conn.close()


def event_ics(uid, *, summary="Test event", location="Berlin Office", start="20260110T090000Z", end="20260110T100000Z"):
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//
BEGIN:VEVENT
UID:{uid}
DTSTART:{start}
DTEND:{end}
SUMMARY:{summary}
LOCATION:{location}
END:VEVENT
END:VCALENDAR
"""
