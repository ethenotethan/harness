"""
Comments pinned to a living artifact.

A reader drops a pin on the rendered artifact and writes a note; the agent
reads the open notes before it edits the artifact and resolves the ones it
has addressed. A comment is the reader's words about the artifact, not part
of it, so it lives beside the content rather than in it:

  ~/.hermes/artifacts/comments/<id>.json   every comment on one artifact

Keeping it out of the artifact record matters for `html`, whose content is
replaced wholesale on each write: a note stored in the document would be
erased by the next edit. For the same reason a comment never appends a
revision or moves `rev` — the history stays an audit of what the artifact
SAID. Each comment records the `rev` it was placed on, so a reader of an
older pin can tell the page has changed underneath it.

An anchor says where the pin sits: a CSS `selector` for the element, `x`/`y`
as fractions of that element's box, and the `text` the element showed. The
text is what lets a pin be found again, or reported as detached, after the
markup around it changes.

Surface (see methods_harness.py): artifact.comment.add / list / resolve /
delete RPCs and an `artifact.comment.changed` event.
"""

import threading
import uuid
from pathlib import Path
from typing import Optional

from tui_gateway.artifact_store import (
    _ID_RE,
    _artifacts_dir,
    _now_iso,
    _read_json,
    _write_json,
    get_artifact,
)

MAX_COMMENTS = 200
MAX_BODY_BYTES = 4000
MAX_SELECTOR_BYTES = 512
MAX_ANCHOR_TEXT_CHARS = 280
MAX_AUTHOR_CHARS = 128

_lock = threading.Lock()


def _comments_file(artifact_id: str) -> Path:
    # The id becomes a file name, so it must be an artifact id and nothing else.
    if not _ID_RE.match(artifact_id):
        raise ValueError("invalid artifact id")
    return _artifacts_dir() / "comments" / f"{artifact_id}.json"


def _plain(value, name: str, limit: int, *, newlines: bool = False) -> str:
    """A bounded string with no control characters (newlines and tabs only
    where a body needs them)."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    allowed = "\n\t" if newlines else ""
    if any((ord(ch) < 32 or ord(ch) == 127) and ch not in allowed for ch in value):
        raise ValueError(f"{name} must not contain control characters")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} chars")
    return value


def _fraction(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"anchor.{name} must be a number")
    if not 0 <= value <= 1:
        raise ValueError(f"anchor.{name} must be between 0 and 1")
    return round(float(value), 4)


def normalize_anchor(anchor) -> dict:
    """The stored form of a pin position. None or {} is a comment on the
    artifact as a whole."""
    if anchor is None:
        return {}
    if not isinstance(anchor, dict):
        raise ValueError("anchor must be an object")
    unknown = set(anchor) - {"selector", "x", "y", "text"}
    if unknown:
        raise ValueError(f"anchor has unknown fields: {sorted(unknown)}")
    stored: dict = {}
    selector = anchor.get("selector")
    if selector:
        selector = _plain(selector, "anchor.selector", MAX_SELECTOR_BYTES).strip()
        if len(selector.encode("utf-8")) > MAX_SELECTOR_BYTES:
            raise ValueError(f"anchor.selector exceeds {MAX_SELECTOR_BYTES} bytes")
        stored["selector"] = selector
    for axis in ("x", "y"):
        if anchor.get(axis) is not None:
            stored[axis] = _fraction(anchor[axis], axis)
    text = anchor.get("text")
    if text:
        stored["text"] = " ".join(_plain(text, "anchor.text", 4 * MAX_ANCHOR_TEXT_CHARS, newlines=True).split())[:MAX_ANCHOR_TEXT_CHARS]
    return stored


def add_comment(
    artifact_id: str,
    body: str,
    author: str = "",
    anchor=None,
    rev: Optional[int] = None,
) -> Optional[dict]:
    """Pin a comment to an artifact. Returns the stored comment, or None when
    the artifact is unknown. ``rev`` is the revision the reader was looking
    at; omitted, it is the current one. Raises ValueError on invalid input."""
    artifact_id = (artifact_id or "").strip()
    text = _plain(body, "body", MAX_BODY_BYTES, newlines=True).strip()
    if not text:
        raise ValueError("body is required")
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(f"body exceeds {MAX_BODY_BYTES} bytes")
    stored_anchor = normalize_anchor(anchor)
    stored_author = _plain(author or "", "author", MAX_AUTHOR_CHARS).strip()
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int) or rev < 0):
        raise ValueError("rev must be a non-negative integer")

    artifact = get_artifact(artifact_id)
    if artifact is None:
        return None
    with _lock:
        comments = _read_json(_comments_file(artifact_id), [])
        if len(comments) >= MAX_COMMENTS:
            raise ValueError(f"comment cap reached ({MAX_COMMENTS}); delete resolved comments first")
        comment = {
            "id": f"c-{uuid.uuid4().hex[:12]}",
            "artifact_id": artifact_id,
            "rev": artifact.get("rev", 0) if rev is None else rev,
            "anchor": stored_anchor,
            "author": stored_author,
            "body": text,
            "created_at": _now_iso(),
            "resolved": False,
        }
        comments.append(comment)
        _write_json(_comments_file(artifact_id), comments)
        return comment


def list_comments(artifact_id: str, include_resolved: bool = True) -> list[dict]:
    """An artifact's comments, oldest first."""
    with _lock:
        comments = _read_json(_comments_file((artifact_id or "").strip()), [])
    if include_resolved:
        return comments
    return [comment for comment in comments if not comment.get("resolved")]


def open_comment_count(artifact_id: str) -> int:
    return len(list_comments(artifact_id, include_resolved=False))


def resolve_comment(
    artifact_id: str,
    comment_id: str,
    resolved: bool = True,
    resolved_by: str = "",
) -> Optional[dict]:
    """Mark a comment addressed (or reopen it). Returns the stored comment, or
    None when the artifact or comment is unknown."""
    artifact_id = (artifact_id or "").strip()
    actor = _plain(resolved_by or "", "resolved_by", MAX_AUTHOR_CHARS).strip()
    with _lock:
        comments = _read_json(_comments_file(artifact_id), [])
        for comment in comments:
            if comment.get("id") != comment_id:
                continue
            if bool(comment.get("resolved")) != bool(resolved):
                comment["resolved"] = bool(resolved)
                if resolved:
                    comment["resolved_at"] = _now_iso()
                    comment["resolved_by"] = actor
                else:
                    comment.pop("resolved_at", None)
                    comment.pop("resolved_by", None)
                _write_json(_comments_file(artifact_id), comments)
            return comment
    return None


def delete_comment(artifact_id: str, comment_id: str) -> bool:
    artifact_id = (artifact_id or "").strip()
    with _lock:
        comments = _read_json(_comments_file(artifact_id), [])
        kept = [comment for comment in comments if comment.get("id") != comment_id]
        if len(kept) == len(comments):
            return False
        _write_json(_comments_file(artifact_id), kept)
        return True


def delete_all_comments(artifact_id: str) -> None:
    """Drop an artifact's comments with the artifact."""
    with _lock:
        _comments_file((artifact_id or "").strip()).unlink(missing_ok=True)
