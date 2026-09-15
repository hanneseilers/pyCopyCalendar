from calendar_sync.state import StateRepository, open_database


def test_fresh_database_gets_schema(tmp_path):
    conn = open_database(tmp_path / "data/sync.sqlite3", backup_dir=tmp_path / "data/backups")
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"schema_version", "source_calendar", "mirror_mapping", "sync_run"} <= tables
    conn.close()


def test_no_backup_created_on_fresh_database(tmp_path):
    backup_dir = tmp_path / "data/backups"
    conn = open_database(tmp_path / "data/sync.sqlite3", backup_dir=backup_dir)
    conn.close()
    assert not backup_dir.exists() or list(backup_dir.glob("*.bak")) == []


def test_reopen_does_not_reapply_migrations_or_lose_data(tmp_path):
    db_path = tmp_path / "data/sync.sqlite3"
    backup_dir = tmp_path / "data/backups"
    conn = open_database(db_path, backup_dir=backup_dir)
    repo = StateRepository(conn)
    repo.record_source_success("dept-a", "https://x/source-a/")
    conn.close()

    conn2 = open_database(db_path, backup_dir=backup_dir)
    row = conn2.execute("SELECT * FROM source_calendar WHERE source_id = 'dept-a'").fetchone()
    assert row is not None
    assert row["calendar_url"] == "https://x/source-a/"
    conn2.close()


def test_record_write_then_get_mapping(tmp_path):
    conn = open_database(tmp_path / "data/sync.sqlite3", backup_dir=tmp_path / "data/backups")
    repo = StateRepository(conn)
    repo.record_source_success("dept-a", "https://x/source-a/")
    repo.start_run("run-1", window_start="2026-01-01T00:00:00Z", window_end="2026-02-01T00:00:00Z", dry_run=False)
    repo.record_write(
        instance_key="dept-a|uid1|SINGLE", source_id="dept-a", source_href="href1", source_uid="uid1",
        recurrence_key="SINGLE", source_etag="se1", target_href="thref1", target_uid="tuid1@calendar-mirror",
        target_etag="te1", fingerprint="fp1", start_utc="2026-01-05T09:00:00Z", end_utc="2026-01-05T10:00:00Z",
        run_id="run-1",
    )
    mapping = repo.get_mapping("dept-a|uid1|SINGLE")
    assert mapping["status"] == "active"
    assert mapping["fingerprint"] == "fp1"
    assert len(repo.all_active_mappings()) == 1
    conn.close()


def test_record_deleted_changes_status(tmp_path):
    conn = open_database(tmp_path / "data/sync.sqlite3", backup_dir=tmp_path / "data/backups")
    repo = StateRepository(conn)
    repo.record_source_success("dept-a", "https://x/source-a/")
    repo.start_run("run-1", window_start="w0", window_end="w1", dry_run=False)
    repo.record_write(
        instance_key="k1", source_id="dept-a", source_href="h", source_uid="u", recurrence_key="SINGLE",
        source_etag=None, target_href="th", target_uid="tu@calendar-mirror", target_etag=None,
        fingerprint="fp", start_utc="s", end_utc="e", run_id="run-1",
    )
    repo.record_deleted("k1")
    assert repo.get_mapping("k1")["status"] == "deleted"
    assert repo.all_active_mappings() == []
    conn.close()


def test_start_and_finish_run_records_counts(tmp_path):
    conn = open_database(tmp_path / "data/sync.sqlite3", backup_dir=tmp_path / "data/backups")
    repo = StateRepository(conn)
    repo.start_run("run-1", window_start="w0", window_end="w1", dry_run=False)
    repo.finish_run("run-1", status="success", created=2, updated=1, deleted=0, unchanged=3)
    row = conn.execute("SELECT * FROM sync_run WHERE run_id = 'run-1'").fetchone()
    assert row["status"] == "success"
    assert row["created_count"] == 2
    assert row["finished_at"] is not None
    conn.close()


def test_backup_created_before_a_real_migration(tmp_path, monkeypatch):
    import calendar_sync.state as state_module

    db_path = tmp_path / "data/sync.sqlite3"
    backup_dir = tmp_path / "data/backups"
    conn = open_database(db_path, backup_dir=backup_dir)
    conn.close()

    # Simulate a future migration being added, forcing open_database() to
    # back up the existing (non-empty) file before applying it.
    extra_migration = (2, ("CREATE TABLE probe (id INTEGER PRIMARY KEY);",))
    monkeypatch.setattr(state_module, "_MIGRATIONS", state_module._MIGRATIONS + [extra_migration])

    conn2 = open_database(db_path, backup_dir=backup_dir)
    backups = list(backup_dir.glob("*.bak"))
    assert len(backups) == 1
    assert conn2.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] == 2
    conn2.close()
