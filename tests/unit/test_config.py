import json

import pytest

from calendar_sync.config import ConfigError, load_config


def base_config(**overrides):
    cfg = {
        "nextcloud": {"base_url": "https://cloud.example.invalid/remote.php/dav/"},
        "sources": [
            {"id": "a", "calendar_url": "https://cloud.example.invalid/remote.php/dav/calendars/user/a/"},
            {"id": "b", "calendar_url": "https://cloud.example.invalid/remote.php/dav/calendars/user/b/"},
        ],
        "target": {"calendar_url": "https://cloud.example.invalid/remote.php/dav/calendars/user/target/"},
        "location_filter": {
            "locations": [{"canonical": "Berlin Office", "aliases": ["Berlin Office", "Office Berlin"]}]
        },
        "window": {"lookback_days": 7, "lookahead_days": 180},
    }
    cfg.update(overrides)
    return cfg


def write_config(tmp_path, data) -> tuple:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.json"
    path.write_text(json.dumps(data))
    return path, tmp_path


def test_loads_valid_config(tmp_path):
    path, root = write_config(tmp_path, base_config())
    config = load_config(path, project_root=root)
    assert [s.id for s in config.sources] == ["a", "b"]
    assert config.location_filter.locations[0].canonical == "Berlin Office"


def test_rejects_duplicate_source_ids(tmp_path):
    cfg = base_config()
    cfg["sources"][1]["id"] = "a"
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(path, project_root=root)


def test_requires_at_least_one_enabled_source(tmp_path):
    cfg = base_config()
    for s in cfg["sources"]:
        s["enabled"] = False
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="enabled"):
        load_config(path, project_root=root)


def test_rejects_target_equal_to_source(tmp_path):
    cfg = base_config()
    cfg["target"]["calendar_url"] = cfg["sources"][0]["calendar_url"]
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="not equal"):
        load_config(path, project_root=root)


def test_rejects_target_nested_inside_source(tmp_path):
    cfg = base_config()
    cfg["target"]["calendar_url"] = cfg["sources"][0]["calendar_url"] + "sub/"
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="overlap"):
        load_config(path, project_root=root)


def test_rejects_target_on_different_origin(tmp_path):
    cfg = base_config()
    cfg["target"]["calendar_url"] = "https://other.invalid/remote.php/dav/calendars/user/target/"
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError):
        load_config(path, project_root=root)


def test_rejects_ambiguous_alias_mapping_to_two_canonicals(tmp_path):
    cfg = base_config()
    cfg["location_filter"]["locations"].append(
        {"canonical": "Hamburg Office", "aliases": ["Office Berlin"]}  # collides after normalization
    )
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="already mapped"):
        load_config(path, project_root=root)


def test_rejects_empty_locations(tmp_path):
    cfg = base_config()
    cfg["location_filter"]["locations"] = []
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError):
        load_config(path, project_root=root)


def test_rejects_storage_path_escaping_project_root(tmp_path):
    cfg = base_config(storage={"database": "../outside.sqlite3"})
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="escapes"):
        load_config(path, project_root=root)


def test_rejects_out_of_range_delete_ratio(tmp_path):
    cfg = base_config(safety={"max_delete_ratio": 1.5})
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError):
        load_config(path, project_root=root)


def test_rejects_unsupported_match_mode(tmp_path):
    cfg = base_config()
    cfg["location_filter"]["match_mode"] = "substring"
    path, root = write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="match_mode"):
        load_config(path, project_root=root)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.json", project_root=tmp_path)


def test_defaults_are_applied(tmp_path):
    path, root = write_config(tmp_path, base_config())
    config = load_config(path, project_root=root)
    assert config.nextcloud.verify_tls is True
    assert config.safety.dry_run_default is True
    assert config.mirroring.copy_organizer is False
    assert config.storage.database == "data/sync.sqlite3"
