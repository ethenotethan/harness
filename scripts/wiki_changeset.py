#!/usr/bin/env python3
"""
Wiki changeset tracking module.

Captures before/after state of wiki pages on every write, stores structured
changeset JSON files, and maintains a chronological index for fast timeline
queries. Integrates with git for raw diff storage.

Storage layout:
  wiki/changesets/
  ├── index.json                    # chronological list of all changesets
  ├── 2026-06-28T143000-001.json    # individual changeset files
  └── ...

Usage from wiki_api.py:
  from scripts.wiki_changeset import wiki_capture_changeset, wiki_query_changesets

Usage from the agent (after writing pages):
  wiki_capture_changeset("entities/llama-cpp.md", "update",
      "Added speculative decoding benchmarks", "ingest",
      "raw/articles/source.md")
"""

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _wiki_root(wiki_path: Optional[str] = None) -> Path:
    """Resolve wiki root path."""
    if wiki_path:
        return Path(os.path.expanduser(wiki_path))
    return Path(os.path.expanduser(os.environ.get("WIKI_PATH", "~/wiki")))


def _changesets_dir(wiki_path: Optional[str] = None) -> Path:
    """Get or create the changesets directory."""
    d = _wiki_root(wiki_path) / "changesets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path(wiki_path: Optional[str] = None) -> Path:
    return _changesets_dir(wiki_path) / "index.json"


def _load_index(wiki_path: Optional[str] = None) -> list:
    """Load the changeset index, or return empty list."""
    ip = _index_path(wiki_path)
    if not ip.exists():
        return []
    try:
        with open(ip) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_index(index: list, wiki_path: Optional[str] = None):
    """Save the changeset index atomically."""
    ip = _index_path(wiki_path)
    tmp = ip.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
    os.replace(tmp, ip)


def _sha256_file(path: Path) -> str:
    """Compute SHA256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _next_changeset_id(wiki_path: Optional[str] = None) -> str:
    """Generate a unique changeset ID: ISO-timestamp-NNN."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    csd = _changesets_dir(wiki_path)
    # Count existing changesets with this timestamp prefix
    existing = list(csd.glob(f"{ts}-*.json"))
    n = len(existing) + 1
    return f"{ts}-{n:03d}"


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter — returns (metadata, body)."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    metadata = {}
    current_key = None
    current_list = None
    for line in parts[1].split("\n"):
        if line.startswith("  - ") and current_key:
            if current_list is None:
                current_list = []
            value = line.strip()[2:].strip().strip('"').strip("'")
            current_list.append(value)
            continue
        if current_key and current_list is not None:
            metadata[current_key] = current_list
            current_key = None
            current_list = None
        line = line.strip()
        if not line:
            current_key = None
            current_list = None
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if val:
                metadata[key] = val
            else:
                current_key = key
                current_list = None
    if current_key and current_list is not None:
        metadata[current_key] = current_list
    return metadata, parts[2]


def wiki_capture_changeset(
    page_path: str,
    action: str,
    summary: str,
    trigger: str = "manual",
    source: str = "",
    wiki_path: Optional[str] = None,
) -> dict:
    """Capture a changeset for a wiki page modification.

    Captures the current state of the page (after the write), computes a
    SHA256 hash, records git commit info, and stores a structured changeset
    JSON file. Updates the chronological index.

    Args:
        page_path: Relative path within wiki (e.g. 'entities/llama-cpp.md')
        action: One of 'create', 'update', 'archive', 'delete'
        summary: Human-readable summary of what changed
        trigger: What triggered this change ('ingest', 'query', 'lint',
                 'process-inbox', 'manual')
        source: Optional source file (e.g. 'raw/articles/source.md')
        wiki_path: Optional wiki root path override

    Returns:
        The changeset dict that was stored, or error dict.
    """
    wiki = _wiki_root(wiki_path)
    target = wiki / page_path

    # Resolve to prevent path traversal
    try:
        target = target.resolve()
        wiki_resolved = wiki.resolve()
    except Exception:
        return {"error": "path resolution failed"}

    if not str(target).startswith(str(wiki_resolved)):
        return {"error": f"path escapes wiki: {page_path}"}

    if action not in ("create", "update", "archive", "delete"):
        return {"error": f"invalid action: {action}"}

    # Compute after-hash (page must exist unless it's a delete)
    after_hash = ""
    diff_stats = {"lines_added": 0, "lines_removed": 0}
    page_title = ""
    page_type = ""

    if target.exists() and target.suffix == ".md":
        after_hash = _sha256_file(target)
        try:
            content = target.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(content)
            page_title = fm.get("title", target.stem)
            page_type = fm.get("type", "concept")
        except Exception:
            page_title = target.stem
    elif action == "delete":
        after_hash = ""
        page_title = target.stem
    else:
        return {"error": f"page not found: {page_path}"}

    # Get git diff stats if git is available
    git_commit = ""
    git_root = wiki
    while git_root != git_root.parent and not (git_root / ".git").exists():
        git_root = git_root.parent

    if (git_root / ".git").exists():
        try:
            # Stage the file
            subprocess.run(
                ["git", "add", str(target)],
                cwd=str(wiki),
                capture_output=True,
                timeout=10,
            )

            # Try to commit; if nothing staged, just grab HEAD
            commit_msg = f"[{action}] {page_path}: {summary}"[:72]
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=str(wiki),
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Get HEAD hash (works whether or not we made a new commit)
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(wiki),
                capture_output=True,
                text=True,
                timeout=5,
            )
            git_commit = hash_result.stdout.strip()[:8]

            # Get diff stats: if we made a new commit, diff HEAD~1..HEAD;
            # otherwise diff against the initial commit for baseline stats
            if result.returncode == 0:
                # New commit was created — diff against parent
                diff_target = "HEAD~1"
            else:
                # Nothing to commit — file hasn't changed since last commit.
                # Diff against the root commit to capture total file size.
                root_hash = subprocess.run(
                    ["git", "rev-list", "--max-parents=0", "HEAD"],
                    cwd=str(wiki),
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                diff_target = root_hash if root_hash else "HEAD~1"

            diff_result = subprocess.run(
                ["git", "diff", "--stat", diff_target, "HEAD", "--", str(target)],
                cwd=str(wiki),
                capture_output=True,
                text=True,
                timeout=5,
            )
            stat_line = diff_result.stdout.strip()
            if "insertion" in stat_line or "deletion" in stat_line:
                import re
                ins = re.search(r"(\d+)\s+insertion", stat_line)
                dels = re.search(r"(\d+)\s+deletion", stat_line)
                diff_stats["lines_added"] = int(ins.group(1)) if ins else 0
                diff_stats["lines_removed"] = int(dels.group(1)) if dels else 0
        except Exception:
            pass

    # Build changeset
    csid = _next_changeset_id(wiki_path)
    now = datetime.now(timezone.utc)

    changeset = {
        "id": csid,
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "page": page_path,
        "title": page_title,
        "type": page_type,
        "summary": summary,
        "diff_stats": diff_stats,
        "trigger": trigger,
        "source": source,
        "git_commit": git_commit,
        "after_sha256": after_hash,
    }

    # Write changeset file
    cs_file = _changesets_dir(wiki_path) / f"{csid}.json"
    with open(cs_file, "w") as f:
        json.dump(changeset, f, indent=2)

    # Update index (prepend — newest first)
    index = _load_index(wiki_path)
    index_entry = {
        "id": csid,
        "timestamp": changeset["timestamp"],
        "action": action,
        "page": page_path,
        "title": page_title,
        "type": page_type,
        "summary": summary,
        "git_commit": git_commit,
    }
    index.insert(0, index_entry)
    _save_index(index, wiki_path)

    return changeset


def wiki_query_changesets(
    wiki_path: Optional[str] = None,
    page: Optional[str] = None,
    action: Optional[str] = None,
    trigger: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Query changesets with optional filters.

    Args:
        wiki_path: Wiki root path override
        page: Filter by page path (e.g. 'entities/llama-cpp.md')
        action: Filter by action ('create', 'update', 'archive', 'delete')
        trigger: Filter by trigger ('ingest', 'query', etc.)
        limit: Max results (default 50, max 200)
        offset: Pagination offset
        since: ISO timestamp, only return changesets after this
        until: ISO timestamp, only return changesets before this

    Returns:
        {"changesets": [...], "total": N, "limit": L, "offset": O}
    """
    index = _load_index(wiki_path)

    # Apply filters
    filtered = []
    for entry in index:
        if page and entry.get("page") != page:
            continue
        if action and entry.get("action") != action:
            continue
        if trigger:
            # Trigger is only in the full changeset, not index.
            # Load full changeset to check.
            cs_file = _changesets_dir(wiki_path) / f"{entry['id']}.json"
            if cs_file.exists():
                try:
                    with open(cs_file) as f:
                        cs = json.load(f)
                    if cs.get("trigger") != trigger:
                        continue
                except Exception:
                    continue
            else:
                continue
        if since and entry.get("timestamp", "") < since:
            continue
        if until and entry.get("timestamp", "") > until:
            continue
        filtered.append(entry)

    total = len(filtered)
    page_slice = filtered[offset : offset + min(limit, 200)]

    # Enrich with full changeset data
    enriched = []
    for entry in page_slice:
        cs_file = _changesets_dir(wiki_path) / f"{entry['id']}.json"
        if cs_file.exists():
            try:
                with open(cs_file) as f:
                    enriched.append(json.load(f))
            except Exception:
                enriched.append(entry)
        else:
            enriched.append(entry)

    return {
        "changesets": enriched,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ── CLI entry point for testing ────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: wiki-changeset.py <capture|query> [...]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "capture":
        # wiki-changeset.py capture <page_path> <action> <summary> [trigger] [source]
        if len(sys.argv) < 4:
            print("usage: wiki-changeset.py capture <page_path> <action> <summary> [trigger] [source]")
            sys.exit(1)
        result = wiki_capture_changeset(
            page_path=sys.argv[2],
            action=sys.argv[3],
            summary=sys.argv[4] if len(sys.argv) > 4 else "",
            trigger=sys.argv[5] if len(sys.argv) > 5 else "manual",
            source=sys.argv[6] if len(sys.argv) > 6 else "",
        )
        print(json.dumps(result, indent=2))
    elif cmd == "query":
        result = wiki_query_changesets(
            page=sys.argv[2] if len(sys.argv) > 2 else None,
            limit=int(sys.argv[3]) if len(sys.argv) > 3 else 50,
        )
        print(json.dumps(result, indent=2))
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)