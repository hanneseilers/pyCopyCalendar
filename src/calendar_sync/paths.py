"""Project-root resolution and path containment.

Every application-controlled file (config, credentials, database, logs,
lock, backups) must live below PROJECT_ROOT regardless of the process's
current working directory - the module is invoked periodically by cron,
which does not guarantee any particular cwd.
"""

from __future__ import annotations

from pathlib import Path

# src/calendar_sync/paths.py -> src/calendar_sync -> src -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class PathEscapesProjectRootError(ValueError):
    """A configured or derived path resolves outside PROJECT_ROOT."""


def resolve_project_path(relative: str | Path, *, root: Path = PROJECT_ROOT) -> Path:
    """Resolve `relative` against `root` and reject any escape from it.

    Rejects absolute paths, `..` traversal, and symlinks that resolve
    outside the project root. The returned path does not need to exist.
    """
    root = root.resolve()
    candidate = Path(relative)
    if candidate.is_absolute():
        raise PathEscapesProjectRootError(
            f"Configured path must be relative to the project root, got absolute path: {relative!r}"
        )

    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathEscapesProjectRootError(
            f"Configured path escapes the project root: {relative!r} -> {resolved}"
        ) from None
    return resolved
