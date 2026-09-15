import pytest

from calendar_sync.paths import PathEscapesProjectRootError, resolve_project_path


def test_resolves_relative_path_under_root(tmp_path):
    resolved = resolve_project_path("data/sync.sqlite3", root=tmp_path)
    assert resolved == (tmp_path / "data" / "sync.sqlite3").resolve()


@pytest.mark.parametrize("escaping", ["../outside.txt", "../../etc/passwd", "data/../../outside.txt"])
def test_rejects_traversal_outside_root(tmp_path, escaping):
    with pytest.raises(PathEscapesProjectRootError):
        resolve_project_path(escaping, root=tmp_path)


def test_rejects_absolute_path(tmp_path):
    with pytest.raises(PathEscapesProjectRootError):
        resolve_project_path("/etc/passwd", root=tmp_path)


def test_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape_link"
    link.symlink_to(outside)
    with pytest.raises(PathEscapesProjectRootError):
        resolve_project_path("escape_link/secret.txt", root=tmp_path)
