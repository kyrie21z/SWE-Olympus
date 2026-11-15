"""Path checks for trusted local adapters; not a hostile-process sandbox."""

from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory


def resolve_scoped(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    p = Path(normalized)
    win = PureWindowsPath(relative)
    if not relative or p.is_absolute() or win.drive or win.root or ".." in p.parts or ":" in relative:
        raise ValueError("path must be relative without traversal, drive or stream")
    base = root.resolve(strict=True)
    target = (base / p).resolve()
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
