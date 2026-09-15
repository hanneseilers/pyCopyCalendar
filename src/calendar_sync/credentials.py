"""Parsing of secrets/nextcloud.env and centralized secret redaction.

The file is parsed as plain `KEY=VALUE` data - never sourced or executed
as shell code - so a malformed or hostile file cannot run commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
REQUIRED_KEYS = ("NEXTCLOUD_USERNAME", "NEXTCLOUD_APP_PASSWORD")


class CredentialsError(ValueError):
    """The credentials file is missing, unreadable, or incomplete."""


@dataclass(frozen=True)
class Credentials:
    username: str
    app_password: str


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_env_file(path: Path) -> dict:
    """Parse a simple `KEY=VALUE` file, one assignment per line.

    Blank lines and lines starting with `#` are ignored. Values may be
    wrapped in matching single or double quotes. No shell expansion,
    substitution, or execution is performed.
    """
    if not path.is_file():
        raise CredentialsError(f"Credentials file not found: {path}")

    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            raise CredentialsError(f"{path}:{lineno}: expected KEY=VALUE, got: {raw_line!r}")
        key, value = match.group(1), _strip_quotes(match.group(2).strip())
        values[key] = value
    return values


def load_credentials(path: Path) -> Credentials:
    values = parse_env_file(path)
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise CredentialsError(
            f"{path}: missing required value(s): {', '.join(missing)}"
        )
    return Credentials(
        username=values["NEXTCLOUD_USERNAME"],
        app_password=values["NEXTCLOUD_APP_PASSWORD"],
    )


class Redactor:
    """Replaces known secret values with a fixed placeholder in text.

    Constructed once from `Credentials` and shared by the logging setup
    and any error-formatting code, so a raw exception message or a
    library's repr can never leak the app password into logs.
    """

    PLACEHOLDER = "***redacted***"

    def __init__(self, secrets: list[str]):
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, self.PLACEHOLDER)
        return text

    @classmethod
    def from_credentials(cls, credentials: Credentials) -> "Redactor":
        return cls([credentials.app_password])
