"""CLI integration tests: exit codes and behavior end-to-end against the
fake DAV transport, run inside an isolated fake project root (so this
never touches the real repo's config/secrets/data directories).
"""

import json

import pytest

from calendar_sync import cli
from fake_dav import FakeObject

from conftest import BASE, TARGET_URL, event_ics


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Point calendar_sync.paths.PROJECT_ROOT (used by cli.py to resolve
    --config and every storage/credentials path) at an isolated tmp_path,
    and pre-create the directories a real deployment would have."""
    import calendar_sync.cli as cli_module
    import calendar_sync.paths as paths_module

    for name in ("config", "secrets", "data", "logs", "run"):
        (tmp_path / name).mkdir()

    monkeypatch.setattr(paths_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli_module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "secrets" / "nextcloud.env").write_text(
        "NEXTCLOUD_USERNAME=testuser\nNEXTCLOUD_APP_PASSWORD=s3cr3t-app-password\n"
    )
    return tmp_path


def write_config(project, **overrides):
    config = {
        "nextcloud": {"base_url": BASE, "user_agent": "calendar-sync-test/1.0"},
        "sources": [{"id": "dept-a", "calendar_url": "https://cloud.example.invalid/remote.php/dav/calendars/user/source-a/"}],
        "target": {"calendar_url": TARGET_URL},
        "location_filter": {"locations": [{"canonical": "Berlin Office", "aliases": ["Berlin Office"]}]},
        "window": {"lookback_days": 7, "lookahead_days": 180},
    }
    config.update(overrides)
    (project / "config" / "config.json").write_text(json.dumps(config))
    return config


def test_validate_config_ok(project, capsys):
    write_config(project)
    rc = cli.main(["--validate-config"])
    assert rc == cli.EXIT_OK
    assert "valid" in capsys.readouterr().out


def test_validate_config_rejects_broken_config(project, capsys):
    config = write_config(project)
    del config["target"]
    (project / "config" / "config.json").write_text(json.dumps(config))
    rc = cli.main(["--validate-config"])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_missing_credentials_file_is_config_error(project):
    write_config(project)
    (project / "secrets" / "nextcloud.env").unlink()
    rc = cli.main(["--dry-run"])
    assert rc == cli.EXIT_CONFIG_ERROR


def test_dry_run_and_apply_via_cli(project, wire_fake_transport, monkeypatch):
    from datetime import datetime, timezone

    src = wire_fake_transport.add_collection("/remote.php/dav/calendars/user/source-a/", "Source A")
    tgt = wire_fake_transport.add_collection("/remote.php/dav/calendars/user/target/", "Target")
    src.objects["/remote.php/dav/calendars/user/source-a/ev1.ics"] = FakeObject(event_ics("ev-1"), '"e1"')
    write_config(project)

    from calendar_sync import reconcile as reconcile_module

    original_run = reconcile_module.run

    def run_with_fixed_now(*args, **kwargs):
        kwargs.setdefault("now", datetime(2026, 1, 5, tzinfo=timezone.utc))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(reconcile_module, "run", run_with_fixed_now)

    rc = cli.main(["--dry-run"])
    assert rc == cli.EXIT_OK
    assert tgt.objects == {}

    rc = cli.main(["--apply"])
    assert rc == cli.EXIT_OK
    assert len(tgt.objects) == 1


def test_app_password_never_appears_in_log_file(project, wire_fake_transport, monkeypatch):
    from datetime import datetime, timezone

    wire_fake_transport.add_collection("/remote.php/dav/calendars/user/source-a/", "Source A")
    wire_fake_transport.add_collection("/remote.php/dav/calendars/user/target/", "Target")
    write_config(project)

    from calendar_sync import reconcile as reconcile_module

    original_run = reconcile_module.run
    monkeypatch.setattr(
        reconcile_module, "run",
        lambda *a, **kw: original_run(*a, **{**kw, "now": datetime(2026, 1, 5, tzinfo=timezone.utc)}),
    )

    cli.main(["--dry-run"])
    log_text = (project / "logs" / "sync.log").read_text()
    assert "s3cr3t-app-password" not in log_text
