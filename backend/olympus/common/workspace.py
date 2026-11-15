"""Path checks for trusted local adapters; not a hostile-process sandbox."""

from contextlib import contextmanager
import os
from pathlib import Path, PureWindowsPath
import stat
from tempfile import TemporaryDirectory


def _resolve(path: Path, *, strict: bool = False) -> Path:
    if os.name == "nt":
        # Restricted Windows tokens can deny the directory handles used by realpath.
        # Inspect EVERY existing ancestor instead; reject ALL reparse points, including
        # junctions, rather than silently trusting an unresolved lexical path.
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if strict:
                    raise
                continue
            if (stat.S_ISLNK(info.st_mode) or
                    getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("reparse points are outside the Windows workspace profile")
        return absolute
    return path.resolve(strict=strict)


def resolve_scoped(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    p = Path(normalized)
    win = PureWindowsPath(relative)
    if not relative or p.is_absolute() or win.drive or win.root or ".." in p.parts or ":" in relative:
        raise ValueError("path must be relative without traversal, drive or stream")
    base = _resolve(root, strict=True)
    target = _resolve(base / p)
    if not target.is_relative_to(base):
        raise ValueError("path resolves outside workspace")
    return target


def scopes_overlap(root: Path, first: str, second: str) -> bool:
    a, b = resolve_scoped(root, first), resolve_scoped(root, second)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


@contextmanager
def temporary_workspace(parent: Path):
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="olympus-", dir=parent.resolve()) as directory:
        yield Path(directory)
