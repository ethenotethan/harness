"""Programmatic changeset capture for out-of-band wiki writes.

The wiki's event feed is a join over ``raw/`` ingestion sources and the
changeset index — and until now the index only gained entries when a write
went through the ``wiki.update`` RPC or when the agent REMEMBERED to run the
capture CLI after editing pages with file tools. There was no programmatic
enforcement on insertion: a page written by the agent's terminal session,
a human editor, or any script simply never appeared in the feed, so the
desktop's wiki timeline rendered only whatever the prompting happened to
capture ("best faith prompting").

This module is the enforcement. A daemon thread snapshots every registered
wiki's page tree and, on each sweep, captures a changeset for any page whose
content changed OUTSIDE the capture machinery — then emits a ``wiki.changed``
gateway event so connected clients know the feed moved.

Dedup, so RPC- and CLI-captured writes don't double-record: before capturing,
the sweeper compares the page's current SHA256 against the ``after_sha256``
of the page's most recent changeset. A match means the write was already
captured by whoever made it; the sweeper just refreshes its snapshot.

The first sweep of each wiki is a BASELINE: it records state without
capturing, so a gateway restart over a 200-page wiki does not mint 200
spurious "auto" changesets. Writes made while the gateway was down are
therefore not back-filled — the enforcement is live-forward, matching what
an event feed is for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Directories under the wiki root that are not pages: changeset storage and
# the raw ingestion sources (raw/ files ARE events — wiki_events reads them
# from disk directly, no changeset needed).
_SKIP_DIRS = {"changesets", "raw", ".git"}

_SWEEP_INTERVAL_S = float(os.environ.get("HERMES_WIKI_WATCH_INTERVAL", "20"))


def _snapshot(wiki_root: Path) -> dict[str, tuple[int, int]]:
    """Relative page path → (mtime_ns, size) for every .md page."""
    out: dict[str, tuple[int, int]] = {}
    if not wiki_root.is_dir():
        return out
    for base, dirs, files in os.walk(wiki_root):
        rel_base = Path(base).relative_to(wiki_root)
        parts = rel_base.parts
        if parts and parts[0] in _SKIP_DIRS:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not (not parts and d in _SKIP_DIRS)]
        for name in files:
            if not name.endswith(".md"):
                continue
            p = Path(base) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(wiki_root))] = (st.st_mtime_ns, st.st_size)
    return out


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _latest_captured_sha(page: str, wiki_root: Path) -> Optional[str]:
    """The ``after_sha256`` of the page's newest changeset, or None."""
    try:
        index_path = wiki_root / "changesets" / "index.json"
        entries = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in entries:  # newest first
            if entry.get("page") == page:
                cs_path = wiki_root / "changesets" / f"{entry.get('id')}.json"
                cs = json.loads(cs_path.read_text(encoding="utf-8"))
                return cs.get("after_sha256")
    except Exception:
        return None
    return None


# A page whose mtime is younger than this is possibly still being written
# (multi-chunk agent edits); leave it for the next sweep so a half-written
# page is never captured as a changeset.
_QUIESCENT_NS = int(3e9)


def sweep_once(
    prev: Optional[dict[str, tuple[int, int]]],
    wiki_root: Path,
    capture: Callable[..., dict],
    now_ns: Optional[int] = None,
) -> tuple[dict[str, tuple[int, int]], list[dict]]:
    """One sweep of one wiki. Returns (new snapshot, captured changesets).

    ``prev is None`` marks the baseline sweep: snapshot only, capture nothing.
    Deletions are NOT captured (the capture helper hashes the post-write file,
    which a delete no longer has) — a deleted page simply leaves the snapshot.
    Pure enough to test directly — the thread below owns time and emission.
    """
    now = now_ns if now_ns is not None else time.time_ns()
    current = _snapshot(wiki_root)
    if prev is None:
        return current, []

    captured: list[dict] = []
    settled = dict(prev)
    for page, stamp in current.items():
        known = prev.get(page)
        if known == stamp:
            settled[page] = stamp
            continue
        if now - stamp[0] < _QUIESCENT_NS:
            # Still (possibly) being written — keep the OLD record so the next
            # sweep re-examines this page instead of silently adopting the
            # half-written state.
            if known is not None:
                settled[page] = known
            else:
                settled.pop(page, None)
            continue
        settled[page] = stamp
        action = "create" if known is None else "update"
        sha = _sha256(wiki_root / page)
        if sha is not None and sha == _latest_captured_sha(page, wiki_root):
            # Already captured by wiki.update or the capture CLI — the write
            # brought its own audit entry; recording it again would double
            # every RPC write in the feed.
            continue
        try:
            cs = capture(
                page_path=page,
                action=action,
                summary=f"auto-captured: page {action} outside wiki.update",
                trigger="auto",
                wiki_path=str(wiki_root),
            )
            if isinstance(cs, dict) and "error" not in cs:
                captured.append(cs)
        except Exception:
            logger.warning("wiki_watch: capture failed for %s", page, exc_info=True)
    # Deleted pages fall out: settled starts from prev, so drop anything no
    # longer on disk.
    settled = {k: v for k, v in settled.items() if k in current}
    return settled, captured


def start_wiki_watcher(
    emit: Callable[[str, str, dict], None],
    wiki_roots: Callable[[], list[str]],
    interval: float = _SWEEP_INTERVAL_S,
) -> threading.Thread:
    """Start the sweeper daemon. ``emit(type, sid, payload)`` matches the
    gateway's ``_emit``; ``wiki_roots`` is called per sweep so registry edits
    take effect without a restart."""

    def _loop() -> None:
        # The version-skew-safe loader from wiki_api (repo copy first, only
        # accepting a module that actually has the capture symbol).
        from tui_gateway.wiki_api import _load_wiki_changeset_module
        wiki_capture_changeset = _load_wiki_changeset_module(
            "wiki_capture_changeset"
        ).wiki_capture_changeset

        states: dict[str, Optional[dict[str, tuple[int, int]]]] = {}
        while True:
            try:
                for root_str in wiki_roots():
                    root = Path(os.path.expanduser(root_str))
                    prev = states.get(root_str)
                    snapshot, captured = sweep_once(prev, root, wiki_capture_changeset)
                    states[root_str] = snapshot
                    if captured:
                        emit("wiki.changed", "", {
                            "wiki": root_str,
                            "pages": [c.get("page") for c in captured],
                            "changesets": [c.get("id") for c in captured],
                        })
            except Exception:
                logger.debug("wiki_watch sweep failed", exc_info=True)
            time.sleep(interval)

    thread = threading.Thread(target=_loop, name="wiki-watch", daemon=True)
    thread.start()
    return thread
