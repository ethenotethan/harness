"""
File serving for the TUI gateway.

Provides file registration and HTTP serving so remote clients (HermesNative
over WebSocket) can download files the agent produces. Files are staged into
a session-scoped directory and served over HTTP with Bearer-token auth.

Paths
-----
Served root:  ~/.hermes/served-files/
Layout:       {session_id}/{file_id}{ext}

Each file gets a short unique ID so that URLs are opaque and don't leak
the original filename to clients that haven't been authenticated yet.
"""

from __future__ import annotations

import hmac
import logging
import mimetypes
import os
import shutil
import uuid
from pathlib import Path

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERVE_ROOT = Path(os.path.expanduser("~/.hermes/served-files"))

# How long a file stays available after the session ends (seconds).
# 1 hour is enough for the user to open the native app and view files
# while being short enough to not accumulate cruft.
FILE_TTL_SECONDS = 3600

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_file(
    session_id: str,
    source_path: str,
    *,
    serve_root: Path | None = None,
    base_url: str = "http://localhost:8642",
) -> dict | None:
    """Copy ``source_path`` into the served directory and return attachment metadata.

    Returns a dict with keys ``id``, ``name``, ``mime_type``, ``size``,
    ``url``, ``disposition``, or ``None`` if the source doesn't exist.
    """
    root = serve_root or DEFAULT_SERVE_ROOT
    src = Path(source_path)
    if not src.exists() or not src.is_file():
        _log.debug("register_file: source not found: %s", source_path)
        return None

    file_id = uuid.uuid4().hex[:8]
    dest_dir = root / session_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / f"{file_id}{src.suffix}"
    shutil.copy2(src, dest)

    mime_type, _ = mimetypes.guess_type(str(src))
    if not mime_type:
        mime_type = "application/octet-stream"

    url = f"{base_url.rstrip('/')}/v1/files/{session_id}/{file_id}{src.suffix}"

    return {
        "id": file_id,
        "name": src.name,
        "mime_type": mime_type,
        "size": src.stat().st_size,
        "url": url,
        "disposition": "inline" if mime_type.startswith("image/") else "attachment",
    }


def resolve_file(
    session_id: str,
    filename: str,
    *,
    serve_root: Path | None = None,
) -> Path | None:
    """Resolve a served file path for the given session and filename.

    Returns the absolute path if the file exists inside the serve root,
    or ``None`` if not found or if path traversal is detected.
    """
    root = (serve_root or DEFAULT_SERVE_ROOT).resolve()
    session_dir = (root / session_id).resolve()

    # Security: ensure the file is inside the serve root.
    target = (session_dir / filename).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        _log.warning("resolve_file: path traversal attempt: %s / %s", session_id, filename)
        return None

    if not target.exists() or not target.is_file():
        return None

    return target


def validate_bearer_token(token: str | None, expected: str) -> bool:
    """Constant-time comparison of a Bearer token against the expected value.

    Returns True if the token matches. If ``expected`` is empty, auth is
    considered disabled and returns True for any token (including None).
    """
    if not expected:
        return True
    if not token:
        return False
    return hmac.compare_digest(token, expected)


def cleanup_stale_files(
    *,
    serve_root: Path | None = None,
    ttl_seconds: int = FILE_TTL_SECONDS,
) -> int:
    """Remove served files that have exceeded their TTL.

    Returns the number of files removed.
    """
    import time

    root = serve_root or DEFAULT_SERVE_ROOT
    if not root.exists():
        return 0

    now = time.time()
    removed = 0

    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        for file_path in session_dir.iterdir():
            if not file_path.is_file():
                continue
            try:
                age = now - file_path.stat().st_mtime
                if age > ttl_seconds:
                    file_path.unlink()
                    removed += 1
            except OSError:
                pass

        # Remove empty session directories
        try:
            remaining = list(session_dir.iterdir())
            if not remaining:
                session_dir.rmdir()
        except OSError:
            pass

    return removed