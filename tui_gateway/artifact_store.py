"""
Living-artifact store: named models in the HermesNative render dialects
(map/chart/graph/stats/table/markdown) that ANY writer maintains — chat
turns, cron jobs, workflows, deterministic code — and connected clients
render live. The writer contract is the fence dialect; the store doesn't
care who produced the content.

Storage:
  ~/.hermes/artifacts/index.json          current state of every artifact
  ~/.hermes/artifacts/revisions/<id>.json revision history per artifact

Surface (see server.py):
  artifact.set / get / list / delete / revisions / revision RPCs, plus an
  `artifact.changed` gateway event on every mutation so clients stream
  updates without polling.

Merge semantics live HERE (server-side) so every writer converges the same
way: `map` artifacts union markers by label (incoming wins conflicts);
every other kind replaces content wholesale. Each mutation appends a
revision (capped) — the audit trail that makes delegating writes to agents
supervisable: who changed what, when, and one-click restore.
"""

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

MAX_ARTIFACTS = 200
MAX_CONTENT_BYTES = 512 * 1024
MAX_REVISIONS = 50
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

_lock = threading.Lock()


def _artifacts_dir() -> Path:
    return Path(get_hermes_home()) / "artifacts"


def _index_file() -> Path:
    return _artifacts_dir() / "index.json"


def _revisions_file(artifact_id: str) -> Path:
    return _artifacts_dir() / "revisions" / f"{artifact_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Merge ────────────────────────────────────────────────────────────────


def _merge_map(existing: str, incoming: str) -> str:
    """Union markers by lowercased label; incoming wins conflicts.

    Top-level fields come from incoming when present, else carry over.
    Unparseable JSON on either side -> incoming (a malformed update must
    never brick the artifact).
    """
    try:
        old = json.loads(existing)
        new = json.loads(incoming)
        if not isinstance(old, dict) or not isinstance(new, dict):
            return incoming
    except (json.JSONDecodeError, TypeError):
        return incoming

    merged = {**old, **new}
    by_label: dict[str, dict] = {}
    order: list[str] = []
    for marker in (old.get("markers") or []) + (new.get("markers") or []):
        if not isinstance(marker, dict):
            continue
        label = str(marker.get("label", "")).strip().lower()
        if not label:
            continue
        if label not in by_label:
            order.append(label)
        by_label[label] = marker  # later (incoming) wins
    merged["markers"] = [by_label[label] for label in order]
    return json.dumps(merged, ensure_ascii=False, sort_keys=True)


def merge_content(kind: str, existing: str, incoming: str) -> str:
    if kind == "map":
        return _merge_map(existing, incoming)
    return incoming


# ── Operations ───────────────────────────────────────────────────────────


def set_artifact(
    artifact_id: str,
    kind: str,
    content: str,
    title: Optional[str] = None,
    updated_by: str = "",
    replace: bool = False,
) -> dict:
    """Upsert an artifact, merging per kind unless replace=True; appends a
    revision. Returns the stored artifact dict (the merged state).
    Raises ValueError on invalid input.
    """
    artifact_id = (artifact_id or "").strip()
    kind = (kind or "").strip().lower()
    if not _ID_RE.match(artifact_id):
        raise ValueError(
            "artifact id must be 1-128 chars of [a-zA-Z0-9._-], starting alphanumeric"
        )
    if not kind:
        raise ValueError("artifact kind required")
    if len(content.encode("utf-8", errors="replace")) > MAX_CONTENT_BYTES:
        raise ValueError(f"content exceeds {MAX_CONTENT_BYTES} bytes")

    with _lock:
        index = _read_json(_index_file(), {})
        existing = index.get(artifact_id)
        if existing is None and len(index) >= MAX_ARTIFACTS:
            raise ValueError(f"artifact cap reached ({MAX_ARTIFACTS})")

        if existing and not replace and existing.get("kind") == kind:
            content = merge_content(kind, existing.get("content", ""), content)

        revisions = _read_json(_revisions_file(artifact_id), [])
        rev = (revisions[-1]["rev"] + 1) if revisions else 1

        stored = {
            "id": artifact_id,
            "kind": kind,
            "title": (title or (existing or {}).get("title") or "").strip(),
            "content": content,
            "rev": rev,
            "updated_at": _now_iso(),
            "updated_by": updated_by or "",
        }
        index[artifact_id] = stored
        _write_json(_index_file(), index)

        revisions.append(
            {
                "rev": rev,
                "content": content,
                "updated_at": stored["updated_at"],
                "updated_by": stored["updated_by"],
            }
        )
        if len(revisions) > MAX_REVISIONS:
            revisions = revisions[-MAX_REVISIONS:]
        _write_json(_revisions_file(artifact_id), revisions)
        return stored


def get_artifact(artifact_id: str) -> Optional[dict]:
    with _lock:
        return _read_json(_index_file(), {}).get((artifact_id or "").strip())


def list_artifacts() -> list[dict]:
    """All artifacts WITHOUT content (list views), newest first."""
    with _lock:
        index = _read_json(_index_file(), {})
    summaries = [
        {key: value for key, value in artifact.items() if key != "content"}
        for artifact in index.values()
    ]
    summaries.sort(key=lambda a: a.get("updated_at", ""), reverse=True)
    return summaries


def list_revisions(artifact_id: str) -> list[dict]:
    """Revision metadata (no content), newest first."""
    with _lock:
        revisions = _read_json(_revisions_file((artifact_id or "").strip()), [])
    return [
        {key: value for key, value in revision.items() if key != "content"}
        for revision in reversed(revisions)
    ]


def get_revision(artifact_id: str, rev: int) -> Optional[dict]:
    with _lock:
        revisions = _read_json(_revisions_file((artifact_id or "").strip()), [])
    for revision in revisions:
        if revision.get("rev") == rev:
            return revision
    return None


def delete_artifact(artifact_id: str) -> bool:
    artifact_id = (artifact_id or "").strip()
    with _lock:
        index = _read_json(_index_file(), {})
        if artifact_id not in index:
            return False
        del index[artifact_id]
        _write_json(_index_file(), index)
        try:
            _revisions_file(artifact_id).unlink(missing_ok=True)
        except OSError:
            pass
        return True
