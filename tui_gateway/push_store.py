"""Device push-token registry for APNs notifications.

Native clients (HermesNative macOS/iOS) register their APNs device tokens via
the ``push.register`` RPC; the gateway fans pushes out to every registered
device. Tokens live in ``~/.hermes/push_tokens.json``.

A token entry:
    {
      "token":       "<hex APNs device token>",
      "platform":    "macos" | "ios",
      "device_name": "Ethen's MacBook Pro",
      "bundle_id":   "com.researchoors.HermesNative.macOS",  # optional override
      "registered":  "2026-07-02T10:00:00+00:00",
      "last_seen":   "2026-07-02T10:00:00+00:00"
    }

Registration is idempotent on ``token`` (re-registering refreshes last_seen
and metadata). Tokens that APNs reports as invalid (410 Unregistered / 400
BadDeviceToken) are pruned by the sender.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

_LOCK = threading.Lock()
MAX_TOKENS = 50


def _store_path() -> Path:
    return Path(get_hermes_home()) / "push_tokens.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> list[dict]:
    p = _store_path()
    if not p.exists():
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write(tokens: list[dict]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register_token(
    token: str,
    platform: str = "macos",
    device_name: str = "",
    bundle_id: Optional[str] = None,
) -> dict:
    """Register (or refresh) a device token. Returns the stored entry."""
    token = (token or "").strip().lower()
    if not token:
        return {"error": "token must be a non-empty string"}
    if platform not in ("macos", "ios"):
        return {"error": f"unknown platform: {platform}"}

    with _LOCK:
        tokens = _read()
        now = _now_iso()
        for entry in tokens:
            if entry.get("token") == token:
                entry["platform"] = platform
                entry["last_seen"] = now
                if device_name:
                    entry["device_name"] = device_name
                if bundle_id:
                    entry["bundle_id"] = bundle_id
                _write(tokens)
                return entry
        entry = {
            "token": token,
            "platform": platform,
            "device_name": device_name,
            "registered": now,
            "last_seen": now,
        }
        if bundle_id:
            entry["bundle_id"] = bundle_id
        tokens.append(entry)
        # Bound the registry — evict the least-recently-seen extras.
        if len(tokens) > MAX_TOKENS:
            tokens.sort(key=lambda t: t.get("last_seen", ""), reverse=True)
            tokens = tokens[:MAX_TOKENS]
        _write(tokens)
        return entry


def unregister_token(token: str) -> bool:
    """Remove a device token. Returns True if it was present."""
    token = (token or "").strip().lower()
    if not token:
        return False
    with _LOCK:
        tokens = _read()
        remaining = [t for t in tokens if t.get("token") != token]
        if len(remaining) == len(tokens):
            return False
        _write(remaining)
        return True


def list_tokens() -> list[dict]:
    """All registered device tokens."""
    with _LOCK:
        return _read()


def prune_token(token: str) -> None:
    """Drop a token APNs reported as dead (410/BadDeviceToken)."""
    unregister_token(token)
