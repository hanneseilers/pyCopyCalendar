import pytest

from calendar_sync.credentials import (
    Credentials,
    CredentialsError,
    Redactor,
    load_credentials,
    parse_env_file,
)


def test_parses_simple_key_value(tmp_path):
    path = tmp_path / "nextcloud.env"
    path.write_text("NEXTCLOUD_USERNAME=alice\nNEXTCLOUD_APP_PASSWORD=s3cr3t\n")
    creds = load_credentials(path)
    assert creds == Credentials(username="alice", app_password="s3cr3t")


def test_ignores_blank_lines_and_comments(tmp_path):
    path = tmp_path / "nextcloud.env"
    path.write_text("# comment\n\nNEXTCLOUD_USERNAME=alice\nNEXTCLOUD_APP_PASSWORD=s3cr3t\n")
    creds = load_credentials(path)
    assert creds.username == "alice"


def test_strips_matching_quotes(tmp_path):
    path = tmp_path / "nextcloud.env"
    path.write_text('NEXTCLOUD_USERNAME="alice"\nNEXTCLOUD_APP_PASSWORD=\'s3cr3t\'\n')
    creds = load_credentials(path)
    assert creds == Credentials(username="alice", app_password="s3cr3t")


def test_missing_file_raises(tmp_path):
    with pytest.raises(CredentialsError):
        load_credentials(tmp_path / "does-not-exist.env")


def test_missing_required_key_raises(tmp_path):
    path = tmp_path / "nextcloud.env"
    path.write_text("NEXTCLOUD_USERNAME=alice\n")
    with pytest.raises(CredentialsError):
        load_credentials(path)


def test_malformed_line_raises(tmp_path):
    path = tmp_path / "nextcloud.env"
    path.write_text("this is not key=value shaped as a whole line??\n")
    with pytest.raises(CredentialsError):
        parse_env_file(path)


def test_never_executed_as_shell(tmp_path):
    """A file containing shell metacharacters must be treated as inert
    text, never sourced/executed."""
    path = tmp_path / "nextcloud.env"
    path.write_text(
        "NEXTCLOUD_USERNAME=alice\n"
        "NEXTCLOUD_APP_PASSWORD=s3cr3t\n"
        "MALICIOUS=$(rm -rf /)\n"
    )
    creds = load_credentials(path)
    assert creds.app_password == "s3cr3t"  # parsed as plain data, no expansion happened


def test_redactor_scrubs_password_from_text():
    redactor = Redactor.from_credentials(Credentials(username="alice", app_password="s3cr3t-pw"))
    message = "Authorization: Basic YWxpY2U6czNjcjN0LXB3 failed for s3cr3t-pw"
    redacted = redactor.redact(message)
    assert "s3cr3t-pw" not in redacted
    assert "***redacted***" in redacted


def test_redactor_does_not_touch_unrelated_text():
    redactor = Redactor.from_credentials(Credentials(username="alice", app_password="s3cr3t-pw"))
    message = "Fetched 12 calendar objects from 'dept-a'"
    assert redactor.redact(message) == message
