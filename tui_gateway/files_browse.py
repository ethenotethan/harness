"""Read-only filesystem browsing for the desktop's Hermes file navigator.

The desktop wanted to click through Hermes-specific files — ``indexing/``,
``scripts/``, the ``~/.hermes`` data home — and read them (source and
markdown) in-app. The gateway had no way to do this: the only file readers
were scoped to a single skill (``skills.get``) or a single wiki page
(``wiki.page``), and the HTTP ``/v1/files`` path only serves files the agent
explicitly staged. Neither can list a directory or reach the repo tree.

This module is the enforcement of the one rule that makes exposing a
filesystem to a network client safe: **containment**. Exactly two roots are
browsable — the harness repo checkout (``repo``) and the Hermes data home
(``hermes``, ``~/.hermes``) — and every path a client names is resolved and
verified to live under its declared root before a single byte is read, the
same ``resolve()`` + ``relative_to(root)`` idiom ``skills.get`` and
``file_serve`` already use. There is no write path here at all.

Pure module: it closes over no server globals, so the ``files.*`` RPC
handlers in ``methods_harness`` stay thin wrappers and the logic below is
tested directly against ``tmp_path`` — the same split ``wiki_watch`` uses.
"""

from __future__ import annotations

import os
from pathlib import Path

# Directories that are never worth browsing: VCS internals, byte-caches,
# dependency trees, tool caches, build output. Pruned during the walk so the
# tree the desktop receives is bounded and free of noise. (A symlinked dir is
# never recursed into regardless — see _build_children.)
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".pytest-cache",
    ".idea",
    ".vscode",
    ".eggs",
    ".tox",
    "dist",
    "build",
    "site-packages",
    ".DS_Store",
}

# Hard cap so a giant file can't blow up the reader. Overridable in tests.
_MAX_READ_BYTES = 1_048_576  # 1 MiB


class FileBrowseError(Exception):
    """A client-facing browse failure carrying a JSON-RPC error code.

    The handler maps ``.code``/``.message`` straight onto ``_err`` so the
    codes below are the contract the desktop sees.
    """

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _repo_root() -> Path:
    """The harness checkout root — where indexing/ and scripts/ live."""
    return Path(__file__).resolve().parents[1]


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def file_roots() -> dict[str, Path]:
    """The allowlisted, containment-checked browse roots: name → abs Path.

    ``repo`` is always present. ``hermes`` is included when the data home
    resolves (it may not exist yet on a fresh install) — never let a missing
    optional root stop the repo root from being browsable.
    """
    roots: dict[str, Path] = {"repo": _repo_root().resolve()}
    try:
        roots["hermes"] = _hermes_home().resolve()
    except Exception:
        pass
    return roots


def _resolve_root(root_name: str) -> Path:
    root = file_roots().get(root_name)
    if root is None:
        raise FileBrowseError(4404, f"unknown root '{root_name}'")
    return root


def _resolve_child(root: Path, rel: str) -> Path:
    """Resolve *rel* under *root* with the containment guard, or raise.

    The empty string is the root itself. Traversal and symlinks pointing
    outside the root are rejected here before any listing or read happens.
    """
    target = (root / rel).resolve() if rel else root
    try:
        target.relative_to(root)
    except ValueError:
        raise FileBrowseError(4020, f"path '{rel}' escapes the root")
    return target


def _has_visible_children(d: Path) -> bool:
    """Whether *d* has any non-pruned entry — drives the disclosure arrow.

    Cheap: returns on the first surviving entry rather than materialising the
    directory, so a big folder doesn't cost a full scan just to know it opens.
    """
    try:
        for entry in os.scandir(d):
            try:
                if entry.is_dir(follow_symlinks=False) and entry.name in _SKIP_DIRS:
                    continue
            except OSError:
                continue
            return True
    except OSError:
        return False
    return False


def _children(base: Path, root: Path) -> list[dict]:
    """Immediate children of *base*, sorted dirs-first then case-insensitively.

    Non-recursive by design: the desktop lazy-loads a directory's contents
    when it expands, so every call is bounded to one level and any path is
    always reachable — a repo with more files than any tree cap can't hide a
    subtree the way an eager depth-first dump would.
    """
    try:
        raw = list(os.scandir(base))
    except OSError:
        return []
    raw.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    out: list[dict] = []
    for entry in raw:
        name = entry.name
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir and name in _SKIP_DIRS:
            continue
        rel = str(Path(entry.path).relative_to(root))
        if is_dir:
            # Symlinked dirs are listed but reported childless — the read/list
            # containment guard refuses anything they'd point outside the root.
            has_kids = (
                False if entry.is_symlink() else _has_visible_children(Path(entry.path))
            )
            out.append(
                {"name": name, "path": rel, "type": "dir", "has_children": has_kids}
            )
        else:
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                size = 0
            out.append({"name": name, "path": rel, "type": "file", "size": size})
    return out


def list_tree(root_name: str, rel_path: str = "") -> dict:
    """RPC payload for ``files.list``: one directory level under a named root.

    ``rel_path`` empty lists the root's top level; otherwise it lists that
    subdirectory. The client walks deeper by calling again with a dir's path.
    """
    root = _resolve_root(root_name)
    base = _resolve_child(root, rel_path)
    if not base.is_dir():
        raise FileBrowseError(4404, f"directory not found: {rel_path or '.'}")
    return {
        "root": root_name,
        "root_path": str(root),
        "path": rel_path,
        "entries": _children(base, root),
    }


def read_within(root: Path, rel_path: str) -> dict:
    """Read one UTF-8 text file under *root*, or raise FileBrowseError.

    The containment check is the whole point: ``(root / rel).resolve()`` then
    ``.relative_to(root)`` rejects both ``../`` traversal and symlinks that
    point outside the root before any read happens.
    """
    if not rel_path:
        raise FileBrowseError(4001, "path is required")
    target = _resolve_child(root, rel_path)
    if not target.is_file():
        raise FileBrowseError(4404, f"file not found: {rel_path}")
    size = target.stat().st_size
    if size > _MAX_READ_BYTES:
        raise FileBrowseError(
            4013, f"file too large ({size} bytes; limit {_MAX_READ_BYTES})"
        )
    data = target.read_bytes()
    # A NUL byte in the head is the cheap, reliable binary sniff editors use.
    if b"\x00" in data[:8192]:
        raise FileBrowseError(4015, "binary file is not viewable")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FileBrowseError(4015, "file is not valid UTF-8 text")
    rel = str(target.relative_to(root))
    return {
        "path": rel,
        "content": content,
        "size": size,
        "read_only": not os.access(target, os.W_OK),
        "language": target.suffix.lstrip(".").lower(),
    }


def read_file(root_name: str, rel_path: str) -> dict:
    """RPC payload for ``files.read``: resolve the named root, then read."""
    root = _resolve_root(root_name)
    payload = read_within(root, rel_path)
    payload["root"] = root_name
    return payload
