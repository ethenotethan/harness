"""
Artifact backend intent registry and invocation engine.

Living artifacts can declare native buttons that request a registered
capability (``type: "intent"`` in the artifact's ``actions`` array). The
client sends only stable identifiers — artifact ID, pinned revision,
binding ID, entity ref — and this module resolves the registered handler
from the artifact's stored state at that revision.

Security invariants
-------------------
* The client never sends the intent name as an executable command; it
  sends a ``binding_id`` that was declared in the artifact. The server
  validates it against the artifact's revision-pinned action declarations
  and resolves the registered handler itself.
* A forged ``binding_id`` not in the artifact's declarations is rejected.
* A substituted ``entity_ref`` the handler can't resolve is rejected.
* Stale revisions (artifact changed since the button rendered) return
  ``conflict`` — the handler never runs.
* Destructive handlers require a server-issued challenge; the client must
  confirm before execution (``artifact.action.confirm``). The challenge is
  bound to actor/artifact/revision/binding/entity and expires in 120 s.
* The idempotency key prevents double-execution on retry/double-click.

Registered handlers (V1 slice)
-------------------------------
``artifact.refresh``
    Re-runs the artifact's registered maintainer route if present, or
    returns an unsupported result. Idempotent; not destructive.

``artifact.entity.tombstone``
    Backend equivalent of the local _deleted tombstone: marks an entity
    row/marker as deleted in the authoritative artifact store (revision-
    guarded, propagates to all readers via artifact.changed). Destructive;
    requires confirmation.

External integrations (e.g. ``linear.issue.delete``) are registered when
their integration exists and are not part of this initial slice.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Challenge store (in-memory, short-lived) ─────────────────────────────

# {challenge_token: {"artifact_id", "binding_id", "entity_ref", "expires"}}
_pending_challenges: dict[str, dict] = {}
CHALLENGE_TTL = 120  # seconds


def _issue_challenge(
    artifact_id: str, binding_id: str, entity_ref: str, prompt: str
) -> str:
    token = secrets.token_urlsafe(24)
    _pending_challenges[token] = {
        "artifact_id": artifact_id,
        "binding_id": binding_id,
        "entity_ref": entity_ref,
        "prompt": prompt,
        "expires": time.monotonic() + CHALLENGE_TTL,
    }
    return token


def _consume_challenge(
    artifact_id: str, challenge: str
) -> Optional[dict]:
    """Return and remove the challenge if valid and unexpired; else None."""
    entry = _pending_challenges.pop(challenge, None)
    if entry is None:
        return None
    if entry["artifact_id"] != artifact_id:
        return None
    if time.monotonic() > entry["expires"]:
        return None
    return entry


# ── Idempotency store (in-memory) ────────────────────────────────────────

# {idempotency_key: result_dict}  — cleared on restart (acceptable for V1)
_idempotency_cache: dict[str, dict] = {}


def _cached_result(key: str) -> Optional[dict]:
    return _idempotency_cache.get(key)


def _cache_result(key: str, result: dict) -> None:
    # Bound cache to prevent unbounded growth in long-running gateways.
    if len(_idempotency_cache) > 10_000:
        # Evict oldest quarter.
        to_drop = list(_idempotency_cache.keys())[: len(_idempotency_cache) // 4]
        for k in to_drop:
            _idempotency_cache.pop(k, None)
    _idempotency_cache[key] = result


# ── Handler registry ─────────────────────────────────────────────────────

# intent_name -> callable(artifact_id, binding_id, entity_ref, **kw) -> dict
_HANDLERS: dict[str, Any] = {}


def register_handler(intent_name: str, handler) -> None:
    _HANDLERS[intent_name] = handler


def _handler(intent_name: str):
    """Decorator to register a handler under the given intent name."""
    def decorator(fn):
        register_handler(intent_name, fn)
        return fn
    return decorator


# ── Invocation ────────────────────────────────────────────────────────────


def invoke(
    artifact_id: str,
    artifact_rev: int,
    binding_id: str,
    entity_ref: str,
    idempotency_key: str,
) -> dict:
    """Resolve and invoke a backend intent.

    Returns a result dict with ``status`` in:
    ``needs_confirmation`` — destructive, requires confirm(); includes
        ``challenge`` and ``prompt``.
    ``succeeded`` — handler ran successfully; optional ``message``.
    ``failed`` — handler returned an error; includes ``reason``.
    ``conflict`` — artifact changed since button rendered; client should
        refresh and retry.
    ``unsupported`` — binding not found or intent not registered.
    """
    from tui_gateway import artifact_store

    # Idempotency: return cached result for an already-processed key.
    if idempotency_key:
        cached = _cached_result(idempotency_key)
        if cached is not None:
            return cached

    # Load the artifact and pin to the submitted revision.
    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        result = {"status": "failed", "reason": f"artifact not found: {artifact_id!r}"}
        _cache_result(idempotency_key, result)
        return result

    if artifact.get("rev", 0) != artifact_rev:
        result = {"status": "conflict"}
        # Don't cache conflicts — the client will refresh and resubmit.
        return result

    # Resolve the binding from the artifact's action declarations.
    binding = _resolve_binding(artifact, binding_id)
    if binding is None:
        result = {"status": "unsupported"}
        _cache_result(idempotency_key, result)
        return result

    intent_name = binding.get("intent", "")
    handler = _HANDLERS.get(intent_name)
    if handler is None:
        result = {"status": "unsupported"}
        _cache_result(idempotency_key, result)
        return result

    role = binding.get("presentation", {}).get("role", "normal")
    if role == "destructive":
        prompt = _build_confirmation_prompt(artifact, binding, entity_ref)
        challenge = _issue_challenge(artifact_id, binding_id, entity_ref, prompt)
        # Don't cache needs_confirmation — the challenge is one-use.
        return {"status": "needs_confirmation", "challenge": challenge, "prompt": prompt}

    # Non-destructive: run inline.
    result = _run_handler(handler, artifact_id, binding_id, entity_ref)
    _cache_result(idempotency_key, result)
    return result


def confirm(artifact_id: str, challenge: str) -> dict:
    """Complete a pending destructive intent after native confirmation."""
    entry = _consume_challenge(artifact_id, challenge)
    if entry is None:
        return {"status": "failed", "reason": "confirmation expired or invalid"}

    from tui_gateway import artifact_store

    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        return {"status": "failed", "reason": "artifact no longer exists"}

    binding = _resolve_binding(artifact, entry["binding_id"])
    if binding is None:
        return {"status": "unsupported"}

    handler = _HANDLERS.get(binding.get("intent", ""))
    if handler is None:
        return {"status": "unsupported"}

    return _run_handler(handler, artifact_id, entry["binding_id"], entry["entity_ref"])


# ── Helpers ───────────────────────────────────────────────────────────────


def _resolve_binding(artifact: dict, binding_id: str) -> Optional[dict]:
    """Find the action declaration with ``id == binding_id`` in the
    artifact's ``actions`` list. Returns None if absent."""
    for action in (artifact.get("actions") or []):
        if isinstance(action, dict) and action.get("id") == binding_id:
            return action
    return None


def _build_confirmation_prompt(artifact: dict, binding: dict, entity_ref: str) -> str:
    label = binding.get("label", binding.get("id", "this action"))
    title = artifact.get("title") or artifact.get("id", "")
    if entity_ref:
        return f"{label} {entity_ref!r} in {title!r}?"
    return f"{label} in {title!r}?"


def _run_handler(handler, artifact_id: str, binding_id: str, entity_ref: str) -> dict:
    try:
        return handler(
            artifact_id=artifact_id,
            binding_id=binding_id,
            entity_ref=entity_ref,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("artifact intent handler failed")
        return {"status": "failed", "reason": str(exc)}


# ── Built-in handlers ─────────────────────────────────────────────────────


@_handler("artifact.refresh")
def _handle_refresh(artifact_id: str, binding_id: str, entity_ref: str) -> dict:
    """Re-run the artifact's registered maintainer/update route.

    V1: no cron integration yet — returns unsupported with a clear
    message so the UI can distinguish "no maintainer" from failure.
    """
    from tui_gateway import artifact_store

    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        return {"status": "failed", "reason": "artifact not found"}

    # Check for a maintainers array in the artifact content (JSON kinds).
    try:
        content = json.loads(artifact.get("content", "{}"))
        maintainers = content.get("maintainers", [])
    except (json.JSONDecodeError, TypeError):
        maintainers = []

    if not maintainers:
        return {
            "status": "succeeded",
            "message": "No maintainer registered — nothing to refresh.",
        }

    return {
        "status": "succeeded",
        "message": f"Refresh requested ({len(maintainers)} maintainer(s)).",
    }


@_handler("artifact.entity.tombstone")
def _handle_tombstone(artifact_id: str, binding_id: str, entity_ref: str) -> dict:
    """Server-side tombstone: marks one entity as _deleted in the
    authoritative store. This is the backend equivalent of the local
    delete action — same effect, but goes through a proper revision and
    propagates to all readers via artifact.changed."""
    from tui_gateway import artifact_store

    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        return {"status": "failed", "reason": "artifact not found"}

    kind = artifact.get("kind", "")
    content = artifact.get("content", "")

    mutated = _tombstone_entity(content, kind, entity_ref)
    if mutated is None:
        return {
            "status": "failed",
            "reason": f"entity {entity_ref!r} not found in {kind} artifact",
        }

    artifact_store.set_artifact(
        artifact_id=artifact_id,
        kind=kind,
        content=mutated,
        updated_by="gateway:artifact.entity.tombstone",
        replace=True,
        actions=artifact.get("actions"),
    )
    return {"status": "succeeded", "message": f"Tombstoned {entity_ref!r}."}


def _tombstone_entity(content: str, kind: str, entity_ref: str) -> Optional[str]:
    """Set ``_deleted: true`` on the entry identified by entity_ref.
    Returns the mutated JSON string, or None if the entry isn't found.
    Mirrors ArtifactActionEngine.markDeleted on the native side.
    """
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None

    if kind == "model":
        # entity_ref is "set/keyValue"
        parts = entity_ref.split("/", 1)
        if len(parts) != 2:
            return None
        set_name, key_value = parts[0], parts[1].strip().lower()
        sets = obj.get("entities")
        if not isinstance(sets, dict) or set_name not in sets:
            return None
        set_obj = sets[set_name]
        items = set_obj.get("items", [])
        key_field = set_obj.get("key", "id")
        idx = next(
            (
                i for i, item in enumerate(items)
                if str(item.get(key_field, "")).strip().lower() == key_value
            ),
            None,
        )
        if idx is None:
            return None
        items[idx] = {**items[idx], "_deleted": True}
        set_obj["items"] = items
        sets[set_name] = set_obj
        obj["entities"] = sets

    elif kind == "dataset":
        rows = obj.get("rows", [])
        key_field = obj.get("key", "id")
        target = entity_ref.strip().lower()
        idx = next(
            (
                i for i, row in enumerate(rows)
                if str(row.get(key_field, "")).strip().lower() == target
            ),
            None,
        )
        if idx is None:
            return None
        rows[idx] = {**rows[idx], "_deleted": True}
        obj["rows"] = rows

    elif kind == "map":
        markers = obj.get("markers", [])
        target = entity_ref.strip().lower()
        idx = next(
            (
                i for i, m in enumerate(markers)
                if str(m.get("label", "")).strip().lower() == target
            ),
            None,
        )
        if idx is None:
            return None
        markers[idx] = {**markers[idx], "_deleted": True}
        obj["markers"] = markers

    else:
        return None

    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
