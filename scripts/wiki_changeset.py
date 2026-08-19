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
        with open(ip, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_index(index: list, wiki_path: Optional[str] = None):
    """Save the changeset index atomically."""
    ip = _index_path(wiki_path)
    tmp = ip.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, sort_keys=True)
    os.replace(tmp, ip)


def _events_path(wiki_path: Optional[str] = None) -> Path:
    """Location of the materialized event log (a machine-layer store).

    Lives alongside the changeset index so the two provenance halves — what
    changed (index.json) and what caused it (events.json) — sit together and
    are backed up, git-tracked, and reasoned about as one unit.
    """
    return _changesets_dir(wiki_path) / "events.json"


def _load_events(wiki_path: Optional[str] = None) -> dict:
    """Load the event store as a {key: record} map, or an empty map.

    Keyed by the event key (the raw source path / opaque source id) because
    the key IS the event's identity: the same source ingested twice is one
    event, not two, so a map dedupes for free and makes upsert O(1).
    """
    ep = _events_path(wiki_path)
    if not ep.exists():
        return {}
    try:
        with open(ep, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_events(events: dict, wiki_path: Optional[str] = None):
    """Persist the event store atomically (temp file + os.replace)."""
    ep = _events_path(wiki_path)
    tmp = ep.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, sort_keys=True)
    os.replace(tmp, ep)


def wiki_record_event(
    key: str,
    kind: Optional[str] = None,
    source_url: Optional[str] = None,
    sha256: Optional[str] = None,
    ingested_at: Optional[str] = None,
    trigger: Optional[str] = None,
    wiki_path: Optional[str] = None,
) -> dict:
    """Upsert an ingestion event into the materialized event log.

    An event is a thing the wiki ingested — a raw snapshot, an article, an
    MPP dump — identified by ``key`` (its raw path or opaque source id). This
    is the write-time counterpart to ``wiki_capture_changeset``: instead of
    reconstructing events after the fact by scanning ``raw/`` (fragile — it
    assumed top-level ``.md`` files with frontmatter, and missed the JSON
    snapshots the real pipeline writes), the ingester declares each event as
    it happens.

    ``key`` is the identity. The first write with a given key CREATES the
    record; later writes are idempotent — they only fill in fields that were
    previously empty, and never clobber a value already recorded. So one
    snapshot that causes 40 page writes records ONE event (upserted 40 times),
    not 40. Re-running a backfill is safe for the same reason.

    Fields other than ``key`` are optional because the write path often can't
    infer them (an ``article:<hash>`` key carries no url on its own); an
    ingester that knows them passes them, and a later call that learns them
    fills the blanks.

    Returns the stored record, or ``{"error": ...}`` for a blank key.
    """
    k = (key or "").strip()
    if not k:
        return {"error": "event key is required"}

    events = _load_events(wiki_path)
    existing = events.get(k)
    if not isinstance(existing, dict):
        existing = {}

    def _pick(new, old):
        # Idempotent fill-forward: a non-empty new value only when there is no
        # non-empty old value. Keeps re-runs and re-emits from clobbering
        # richer data recorded earlier.
        new_s = new.strip() if isinstance(new, str) else (new or "")
        old_s = old.strip() if isinstance(old, str) else (old or "")
        return new_s if not old_s and new_s else (old or "")

    record = {
        "key": k,
        "kind": _pick(kind, existing.get("kind")),
        "source_url": _pick(source_url, existing.get("source_url")),
        "sha256": _pick(sha256, existing.get("sha256")),
        "ingested_at": _pick(ingested_at, existing.get("ingested_at")),
        "trigger": _pick(trigger, existing.get("trigger")),
        # First time this key was recorded, so a genuinely undated event still
        # has a real instant to sort by (distinct from ingested_at, which is
        # when the SOURCE says it was ingested).
        "recorded_at": existing.get("recorded_at")
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    events[k] = record
    _save_events(events, wiki_path)
    return record


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


def normalize_provenance(
    source: str = "",
    source_events: Optional[list] = None,
) -> list:
    """Normalize the two provenance inputs into one ordered list of event keys.

    An event key is the wiki-relative path of the raw source that caused the
    change (``raw/articles/llama-cpp-release.md``). Raw sources are immutable
    files, so the path is a stable identity and the file itself already carries
    the event's URL and ingest time — provenance needs no new storage, only the
    edge.

    ``source`` is the legacy single-value form and is folded in as the first
    key, so every existing caller keeps working and gains a list for free.
    Blanks and duplicates are dropped: an empty list and an absent field mean
    the same thing to a reader, and the client collapses both to ``unknown``.
    """
    keys: list[str] = []
    candidates = [source] if isinstance(source, str) else []
    if isinstance(source_events, (list, tuple)):
        candidates.extend(source_events)
    elif isinstance(source_events, str):
        # A single string where a list was expected — the shape a shell caller
        # or a JSON-lite client most easily produces. Accept it rather than
        # silently recording nothing.
        candidates.append(source_events)
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        key = candidate.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def wiki_capture_changeset(
    page_path: str,
    action: str,
    summary: str,
    trigger: str = "manual",
    source: str = "",
    source_events: Optional[list] = None,
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
        source: Legacy single source file (e.g. 'raw/articles/source.md').
                Folded into source_event_keys as the first entry.
        source_events: The events that caused this change, as wiki-relative
                raw source paths. A synthesis usually has several, which the
                single ``source`` could never express.
        wiki_path: Optional wiki root path override

    Returns:
        The changeset dict that was stored, or error dict.

    Provenance is recorded, never inferred. A capture that declares no events
    stores an empty ``source_event_keys``, which reads downstream as *unknown*
    — "nobody recorded this" — not as "nothing caused it". Those two are
    indistinguishable here, so the honest move is to refuse to guess and let
    the count of unknowns shrink as callers start declaring.
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
        # The provenance edge: which ingestion events caused this change.
        # Always present (possibly empty) so a reader never has to distinguish
        # "field missing because old" from "field missing because unrecorded".
        "source_event_keys": normalize_provenance(source, source_events),
        "git_commit": git_commit,
        "after_sha256": after_hash,
    }

    # Write changeset file
    cs_file = _changesets_dir(wiki_path) / f"{csid}.json"
    with open(cs_file, "w", encoding="utf-8") as f:
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

    # Emit an event record for each source that caused this change (create-if-
    # absent, idempotent). This is the write-time capture that replaces the
    # old after-the-fact raw/ scan: one snapshot causing 40 page writes
    # upserts ONE event 40 times, not 40 events. The write path can't infer an
    # event's kind/url from an opaque key, so it records only what it knows —
    # the key and the trigger — and leaves an ingester (or a later call with
    # --event-kind/--event-url) to enrich the rest.
    for event_key in changeset["source_event_keys"]:
        try:
            wiki_record_event(event_key, trigger=trigger, wiki_path=wiki_path)
        except Exception:
            # A capture that succeeded must not fail because the event log
            # hiccuped; the changeset is the source of truth and a backfill
            # can reconstruct any missed event from it.
            pass

    return changeset


def wiki_changeset_diff(changeset_id: str, wiki_path: Optional[str] = None) -> dict:
    """Return the unified git diff for a single changeset.

    Uses the ``git_commit`` recorded at capture time: the capture path commits
    each page write, so ``git show <commit> -- <page>`` reproduces exactly what
    changed. Returns ``{"diff": <unified diff>, "changeset": {...}}`` or
    ``{"error": ...}`` when the changeset is unknown or the wiki has no git
    history (older captures with empty git_commit).
    """
    csid = (changeset_id or "").strip()
    # IDs are timestamp-shaped (e.g. 2026-06-28T140819-001); reject separators
    # so a crafted id can't traverse out of the changesets dir.
    if not csid or "/" in csid or "\\" in csid or ".." in csid:
        return {"error": f"invalid changeset id: {changeset_id!r}"}

    cs_file = _changesets_dir(wiki_path) / f"{csid}.json"
    if not cs_file.exists():
        return {"error": f"changeset not found: {csid}"}
    try:
        with open(cs_file, encoding="utf-8") as f:
            changeset = _with_provenance(json.load(f))
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"changeset unreadable: {exc}"}

    commit = (changeset.get("git_commit") or "").strip()
    page = changeset.get("page", "")
    if not commit:
        return {
            "error": "no git commit recorded for this changeset "
            "(wiki was not git-initialized at capture time)",
            "changeset": changeset,
        }

    wiki = _wiki_root(wiki_path)
    try:
        # --format="" drops the commit header, leaving just the diff body;
        # scoping to the page keeps a multi-file commit focused.
        result = subprocess.run(
            ["git", "show", "--format=", "--no-color", commit, "--", page],
            cwd=str(wiki),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"error": f"git show failed: {exc}", "changeset": changeset}

    if result.returncode != 0:
        return {
            "error": f"git show failed: {result.stderr.strip()[:200]}",
            "changeset": changeset,
        }

    diff = result.stdout
    # Cap pathological diffs; the client renders line-by-line.
    if len(diff) > 200_000:
        diff = diff[:200_000] + "\n… (diff truncated at 200KB)\n"

    return {"diff": diff, "changeset": changeset}


def _with_provenance(changeset: dict) -> dict:
    """Ensure a changeset read from disk carries ``source_event_keys``.

    Changesets written before provenance existed have a ``source`` string and
    no list. Deriving the list on read migrates them in place, at no cost and
    with no rewrite pass: a KB adopting this needs no migration script, only a
    newer gateway. Everything with neither field reads as an empty list, which
    the client renders as *unknown*.
    """
    if not isinstance(changeset, dict):
        return changeset
    if isinstance(changeset.get("source_event_keys"), list):
        return changeset
    enriched = dict(changeset)
    enriched["source_event_keys"] = normalize_provenance(
        changeset.get("source", "") or ""
    )
    return enriched


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
                    with open(cs_file, encoding="utf-8") as f:
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
                with open(cs_file, encoding="utf-8") as f:
                    enriched.append(_with_provenance(json.load(f)))
            except Exception:
                enriched.append(_with_provenance(entry))
        else:
            enriched.append(_with_provenance(entry))

    return {
        "changesets": enriched,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _parse_event_time(value: str) -> Optional["datetime"]:
    """Parse a timestamp into an aware UTC datetime, or None if unparseable.

    Accepts strict RFC3339, a bare ``datetime.isoformat()`` (no zone), a space
    separator, microseconds, a plain date, and a trailing ``Z``. A value with
    no zone is read as UTC. This mirrors the tolerance the raw-scan path needed
    for hand-written ``ingested`` fields, kept here so an event's timestamp is
    normalized the same way whether it came from an ingester or a backfill.
    """
    text = (value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _normalize_event_time(value: str) -> str:
    """Render a timestamp as strict RFC3339 UTC, or "" if unparseable."""
    parsed = _parse_event_time(value)
    if parsed is None:
        return ""
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _caused_by_key(wiki_path: Optional[str] = None) -> dict:
    """Map each event key → the list of changesets that declared it as a cause.

    Built once from the changeset index (paging past the 200-per-call cap) so
    the event→changeset join stays linear. Each caused entry is a compact
    changeset summary the client can navigate to.
    """
    caused: dict = {}
    offset = 0
    while True:
        page = wiki_query_changesets(wiki_path=wiki_path, limit=200, offset=offset)
        rows = page.get("changesets", [])
        for changeset in rows:
            for key in changeset.get("source_event_keys") or []:
                caused.setdefault(key, []).append(
                    {
                        "id": changeset.get("id", ""),
                        "page": changeset.get("page", ""),
                        "title": changeset.get("title", ""),
                        "action": changeset.get("action", ""),
                        "timestamp": changeset.get("timestamp", ""),
                    }
                )
        offset += len(rows)
        if not rows or offset >= page.get("total", 0):
            break
    return caused


def wiki_query_events(
    wiki_path: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Read the materialized event log, joined to the changesets each caused.

    This is the live read path: it serves the emitted event records (written
    at tool-call time by ``wiki_record_event`` / ``wiki_capture_changeset``),
    NOT a reconstruction by scanning ``raw/``. The raw scan is kept only as a
    one-time backfill seed, because the real pipeline writes JSON snapshots in
    subdirs that the scan never saw.

    Each event's ``timestamp`` is strict RFC3339 UTC, taken from
    ``ingested_at`` when present and falling back to ``recorded_at`` (the
    instant the event was first materialized), which is flagged with
    ``time_estimated``. Window bounds are compared as instants. Newest first.

    Returns ``{"events": [...], "total": N, "limit": L, "offset": O}``.
    """
    events_map = _load_events(wiki_path)
    caused = _caused_by_key(wiki_path)

    since_dt = _parse_event_time(since or "")
    until_dt = _parse_event_time(until or "")

    rows: list[dict] = []
    for key, rec in events_map.items():
        if not isinstance(rec, dict):
            continue
        event_kind = str(rec.get("kind", "") or "").strip()
        if kind and event_kind != kind:
            continue

        ingested = str(rec.get("ingested_at", "") or "")
        event_dt = _parse_event_time(ingested)
        time_estimated = False
        if event_dt is None:
            # Fall back to when the event was first recorded — a real instant,
            # but not the source's own ingest time, so flag it as estimated.
            event_dt = _parse_event_time(str(rec.get("recorded_at", "") or ""))
            time_estimated = event_dt is not None

        if since_dt and event_dt and event_dt < since_dt:
            continue
        if until_dt and event_dt and event_dt > until_dt:
            continue

        timestamp = (
            event_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if event_dt
            else ""
        )
        rows.append(
            {
                "key": rec.get("key", key),
                "kind": event_kind,
                "title": str(rec.get("title", "") or key),
                "timestamp": timestamp,
                "time_estimated": time_estimated,
                "source_url": str(rec.get("source_url", "") or ""),
                "sha256": str(rec.get("sha256", "") or ""),
                "trigger": str(rec.get("trigger", "") or ""),
                "changesets": caused.get(key, []),
            }
        )

    # Newest first; an empty timestamp sorts last under reverse ordering, so a
    # genuinely undated event lands at the end rather than dated to now.
    rows.sort(key=lambda e: e["timestamp"], reverse=True)

    total = len(rows)
    window = rows[offset : offset + min(max(limit, 0), 1000)]
    return {"events": window, "total": total, "limit": limit, "offset": offset}


def wiki_backfill_events(wiki_path: Optional[str] = None) -> dict:
    """Materialize an event record per distinct source key across all changesets.

    A one-time (idempotent) migration for a wiki whose events were previously
    only *derivable* from the changeset index. Walks every changeset — paging
    past the 200-per-call cap — collects each distinct ``source_event_keys``
    entry, and upserts it via ``wiki_record_event`` (create-if-absent, so
    re-running never duplicates or clobbers). The write path can't infer a
    key's kind/url, so backfilled records carry only key + trigger; an
    ingester enriches the rest on its next real write.

    Returns ``{"scanned_changesets": N, "distinct_keys": K, "created": C,
    "already_present": P}``.
    """
    caused = _caused_by_key(wiki_path)
    before = set(_load_events(wiki_path).keys())

    scanned = 0
    offset = 0
    while True:
        page = wiki_query_changesets(wiki_path=wiki_path, limit=200, offset=offset)
        rows = page.get("changesets", [])
        scanned += len(rows)
        offset += len(rows)
        if not rows or offset >= page.get("total", 0):
            break

    created = 0
    already = 0
    for key, changesets in caused.items():
        # The earliest changeset that names this key gives a plausible ingest
        # time when nothing better is known — better than the backfill's own
        # clock, since it reflects when the effect actually landed.
        stamps = [c.get("timestamp", "") for c in changesets if c.get("timestamp")]
        earliest = min(stamps) if stamps else None
        if key in before:
            already += 1
        else:
            created += 1
        wiki_record_event(
            key,
            ingested_at=earliest,
            wiki_path=wiki_path,
        )

    return {
        "scanned_changesets": scanned,
        "distinct_keys": len(caused),
        "created": created,
        "already_present": already,
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
            # Remaining args are additional event keys, so a synthesis drawing
            # on several sources can be captured in one call.
            source_events=list(sys.argv[7:]),
        )
        print(json.dumps(result, indent=2))
    elif cmd == "query":
        result = wiki_query_changesets(
            page=sys.argv[2] if len(sys.argv) > 2 else None,
            limit=int(sys.argv[3]) if len(sys.argv) > 3 else 50,
        )
        print(json.dumps(result, indent=2))
    elif cmd == "record":
        # wiki-changeset.py record <key> [kind] [source_url] [sha256] [ingested_at] [trigger]
        if len(sys.argv) < 3:
            print("usage: wiki-changeset.py record <key> [kind] [source_url] [sha256] [ingested_at] [trigger]")
            sys.exit(1)
        result = wiki_record_event(
            key=sys.argv[2],
            kind=sys.argv[3] if len(sys.argv) > 3 else None,
            source_url=sys.argv[4] if len(sys.argv) > 4 else None,
            sha256=sys.argv[5] if len(sys.argv) > 5 else None,
            ingested_at=sys.argv[6] if len(sys.argv) > 6 else None,
            trigger=sys.argv[7] if len(sys.argv) > 7 else None,
        )
        print(json.dumps(result, indent=2))
    elif cmd == "events":
        result = wiki_query_events(
            limit=int(sys.argv[2]) if len(sys.argv) > 2 else 200,
        )
        print(json.dumps(result, indent=2))
    elif cmd == "backfill-events":
        result = wiki_backfill_events()
        print(json.dumps(result, indent=2))
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)