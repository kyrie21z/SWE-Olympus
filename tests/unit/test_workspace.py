import os
import subprocess

import pytest

from olympus.common.workspace import resolve_scoped, scopes_overlap, temporary_workspace


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "a/../../outside",
        "/tmp/x",
        "C:\\temp\\x",
        "C:relative",
        "\\\\server\\share",
        "a:stream",
        "",
    ],
)
def test_path_escape_rejected(workspace, path):
    with pytest.raises(ValueError):
        resolve_scoped(workspace, path)


def test_normalization_and_overlap(workspace):
    assert resolve_scoped(workspace, "src\\a.py") == workspace / "src" / "a.py"
    assert scopes_overlap(workspace, "src", "src/a.py")
    assert scopes_overlap(workspace, "src/a.py", "src")
    assert not scopes_overlap(workspace, "src", "src2")


def test_link_escape(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # Junctions do not require Windows developer mode. Paths are pytest-created.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if result.returncode:
            pytest.skip("host does not permit symlinks or junctions")
    with pytest.raises(ValueError):
        resolve_scoped(workspace, "escape/file.txt")


def test_workspace_cleanup(tmp_path):
    with temporary_workspace(tmp_path) as path:
        (path / "example").write_text("fixture")
    assert not path.exists()
