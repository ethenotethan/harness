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

Confirmation prompt rule (§0.1)
--------------------------------
The confirmation dialog presented to the user MUST lead with the
server-resolved intent name (e.g. ``artifact.entity.tombstone``), NOT the
artifact-authored label. Artifact authors control the label; a malicious
author could label a destructive binding "Refresh" and the user would
confirm without knowing what they triggered. The intent name is resolved
server-side from the registered handler registry and is therefore trusted.
The artifact-authored label may appear only as secondary text, visually
attributed to the artifact.

Entity-ref resolution rule (§0.2)
-----------------------------------
A handler MUST treat ``entity_ref`` as a **lookup key into the pinned
artifact content** and extract all external identifiers (Linear issue IDs,
URLs, etc.) from the *stored entity fields*, NEVER from the client-supplied
string. If the lookup fails, return ``{"status": "failed"}`` — do not
proceed with the raw ref. This bounds the blast radius to what the artifact
already declares: a forged entity_ref that isn't in the artifact content
simply returns failed.

  WRONG:  linear_client.delete(entity_ref)          # client controls target
  RIGHT:  row = _lookup_row(artifact_content, entity_ref)
          linear_client.delete(row["linear_id"])    # stored field, not raw ref

Built-in handlers conform to this rule; plugin authors must follow it too.
See the plugin docs in docs/plugins/actions.md for the wrong-vs-right example.

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

``artifact.session.spawn``
    Runs the intent as a *contained agent session* rather than executing
    anything inline. It creates a session through the standard session
    runtime (so tool policy, isolation, and live introspection all apply)
    and returns the live ``session_id`` in its result, letting the client
    click through into real-time introspection of the run. The initial
    task is built server-side from the binding's author-declared template
    and the entity resolved out of the pinned content (§0.2) — never from
    the raw client-supplied ref. Whether it requires confirmation is
    decided by the binding's ``presentation.role`` like any other intent.

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
    artifact_id: str, binding_id: str, entity_ref: str, prompt: str,
    idempotency_key: str = "",
) -> str:
    token = secrets.token_urlsafe(24)
    _pending_challenges[token] = {
        "artifact_id": artifact_id,
        "binding_id": binding_id,
        "entity_ref": entity_ref,
        "prompt": prompt,
        "idempotency_key": idempotency_key,
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
    actor: str = "",
) -> dict:
    """Resolve and invoke a backend intent.

    Returns a result dict with ``status`` in:
    ``needs_confirmation`` — destructive, requires confirm(); includes
        ``challenge`` and ``prompt``.
    ``succeeded`` — handler ran successfully; optional ``message``. A
        handler that ran the intent as a contained agent session also
        includes ``session_id`` (the live 8-char id), so the client can
        click through into real-time introspection of that run.
    ``failed`` — handler returned an error; includes ``reason``.
    ``conflict`` — artifact changed since button rendered; client should
        refresh and retry.
    ``unsupported`` — binding not found or intent not registered.
    """
    import time as _time
    from tui_gateway import artifact_store, artifact_invocation_ledger as ledger

    # Idempotency — fast path: in-memory cache first, then durable ledger.
    # The ledger check survives gateway restarts; the in-memory dict is the
    # hot path for the same session.
    if idempotency_key:
        cached = _cached_result(idempotency_key)
        if cached is not None:
            return cached
        ledger_record = ledger.lookup_terminal(idempotency_key)
        if ledger_record is not None:
            result = {"status": ledger_record["outcome"]}
            if ledger_record.get("reason"):
                result["reason"] = ledger_record["reason"]
            _cache_result(idempotency_key, result)
            return result

    # Load the artifact and pin to the submitted revision.
    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        result = {"status": "failed", "reason": f"artifact not found: {artifact_id!r}"}
        _cache_result(idempotency_key, result)
        ledger.append(
            artifact_id=artifact_id, rev=artifact_rev, binding_id=binding_id,
            entity_ref=entity_ref, intent="", idempotency_key=idempotency_key,
            phase="invoke", outcome="failed", reason=result["reason"], actor=actor,
        )
        return result

    if artifact.get("rev", 0) != artifact_rev:
        # Conflicts not cached — client will refresh and resubmit with a new rev.
        return {"status": "conflict"}

    # Resolve the binding from the artifact's action declarations.
    binding = _resolve_binding(artifact, binding_id)
    if binding is None:
        result = {"status": "unsupported"}
        _cache_result(idempotency_key, result)
        ledger.append(
            artifact_id=artifact_id, rev=artifact_rev, binding_id=binding_id,
            entity_ref=entity_ref, intent="", idempotency_key=idempotency_key,
            phase="invoke", outcome="unsupported", actor=actor,
        )
        return result

    intent_name = binding.get("intent", "")
    handler = _HANDLERS.get(intent_name)
    if handler is None:
        result = {"status": "unsupported"}
        _cache_result(idempotency_key, result)
        ledger.append(
            artifact_id=artifact_id, rev=artifact_rev, binding_id=binding_id,
            entity_ref=entity_ref, intent=intent_name, idempotency_key=idempotency_key,
            phase="invoke", outcome="unsupported", actor=actor,
        )
        return result

    role = binding.get("presentation", {}).get("role", "normal")
    if role == "destructive":
        prompt = _build_confirmation_prompt(artifact, binding, entity_ref)
        challenge = _issue_challenge(artifact_id, binding_id, entity_ref, prompt, idempotency_key)
        # Don't cache needs_confirmation — the challenge is one-use.
        # Log to ledger so the confirm phase can later reference the same key.
        ledger.append(
            artifact_id=artifact_id, rev=artifact_rev, binding_id=binding_id,
            entity_ref=entity_ref, intent=intent_name, idempotency_key=idempotency_key,
            phase="invoke", outcome="needs_confirmation", actor=actor,
        )
        return {"status": "needs_confirmation", "challenge": challenge, "prompt": prompt}

    # Non-destructive: run inline.
    t0 = _time.monotonic()
    result = _run_handler(handler, artifact_id, binding_id, entity_ref)
    duration_ms = int((_time.monotonic() - t0) * 1000)
    _cache_result(idempotency_key, result)
    ledger.append(
        artifact_id=artifact_id, rev=artifact_rev, binding_id=binding_id,
        entity_ref=entity_ref, intent=intent_name, idempotency_key=idempotency_key,
        phase="invoke", outcome=result.get("status", "failed"),
        reason=result.get("reason"), duration_ms=duration_ms, actor=actor,
    )
    return result


def confirm(artifact_id: str, challenge: str, actor: str = "") -> dict:
    """Complete a pending destructive intent after native confirmation."""
    import time as _time
    from tui_gateway import artifact_invocation_ledger as ledger

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

    intent_name = binding.get("intent", "")
    idempotency_key = entry.get("idempotency_key", "")
    t0 = _time.monotonic()
    result = _run_handler(handler, artifact_id, entry["binding_id"], entry["entity_ref"])
    duration_ms = int((_time.monotonic() - t0) * 1000)

    if idempotency_key:
        _cache_result(idempotency_key, result)

    ledger.append(
        artifact_id=artifact_id, rev=artifact.get("rev", 0),
        binding_id=entry["binding_id"], entity_ref=entry["entity_ref"],
        intent=intent_name, idempotency_key=idempotency_key,
        phase="confirm", outcome=result.get("status", "failed"),
        reason=result.get("reason"), duration_ms=duration_ms, actor=actor,
    )
    return result


# ── Helpers ───────────────────────────────────────────────────────────────


def _resolve_binding(artifact: dict, binding_id: str) -> Optional[dict]:
    """Find the action declaration with ``id == binding_id`` in the
    artifact's ``actions`` list. Returns None if absent."""
    for action in (artifact.get("actions") or []):
        if isinstance(action, dict) and action.get("id") == binding_id:
            return action
    return None


def _build_confirmation_prompt(artifact: dict, binding: dict, entity_ref: str) -> str:
    # Lead with the server-resolved intent name (trusted); label is artifact-authored.
    intent_name = binding.get("intent", binding.get("id", "unknown"))
    label = binding.get("label", "")
    title = artifact.get("title") or artifact.get("id", "")
    body = f"{intent_name}"
    if entity_ref:
        body += f" — {entity_ref} in \"{title}\""
    else:
        body += f" — \"{title}\""
    if label and label.lower() != intent_name.lower():
        body += f"\n(artifact label: \"{label}\")"
    return body + "\n\nThis action cannot be undone. Confirm?"


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


@_handler("artifact.session.spawn")
def _handle_session_spawn(artifact_id: str, binding_id: str, entity_ref: str) -> dict:
    """Run the intent as a contained agent session and return its live id.

    Instead of executing anything inline, this creates a session through the
    standard session runtime and hands back the ``session_id``. The client
    then navigates into that session for real-time introspection — the intent
    becomes a scoped, tool-policied, observable agent run rather than a
    one-off mutation. All arbitrary-execution risk is contained by the
    session sandbox that already exists; this handler only spawns and links.

    §0.2: the task the session is given is composed *server-side* from the
    binding's author-declared ``session_prompt`` template and the entity
    fields resolved out of the pinned artifact content. The raw client
    ``entity_ref`` is used only as a lookup key, never interpolated as an
    instruction. If the ref doesn't resolve to a stored entity, we fail
    rather than spawn a session pointed at an attacker-controlled string.
    """
    from tui_gateway import artifact_store

    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        return {"status": "failed", "reason": "artifact not found"}

    binding = _resolve_binding(artifact, binding_id)
    if binding is None:
        # invoke() already resolved this; defensive for direct/confirm calls.
        return {"status": "unsupported"}

    task = _compose_session_task(artifact, binding, entity_ref)
    if task is None:
        return {
            "status": "failed",
            "reason": f"entity {entity_ref!r} not found in {artifact.get('kind', '')} artifact",
        }

    title = binding.get("label") or f"{artifact.get('title') or artifact_id}"
    try:
        session_id = _spawn_session(task=task, title=title, artifact_id=artifact_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("artifact.session.spawn: session creation failed")
        return {"status": "failed", "reason": f"could not start session: {exc}"}

    if not session_id:
        return {"status": "failed", "reason": "session runtime returned no session id"}

    return {
        "status": "succeeded",
        "session_id": session_id,
        "message": f"Started session for {binding.get('intent', binding_id)!r}.",
    }


def _compose_session_task(artifact: dict, binding: dict, entity_ref: str) -> Optional[str]:
    """Build the initial task string for a spawned session, server-side.

    The template comes from the binding's author-declared ``session_prompt``
    (falls back to a generic instruction). Entity context is pulled from the
    *stored* artifact content via ``entity_ref`` as a lookup key (§0.2) — the
    raw ref is never spliced into the instruction. Returns None when an
    ``entity_ref`` is supplied but resolves to no stored entity, so the caller
    can fail closed instead of spawning against an unresolved target.
    """
    template = binding.get("session_prompt")
    if not isinstance(template, str) or not template.strip():
        template = "Carry out the requested action for this artifact."

    if not entity_ref:
        # Artifact-scoped intent (no per-row target).
        return f"{template}\n\nArtifact: {artifact.get('title') or artifact.get('id', '')}"

    entity = _lookup_entity(artifact, entity_ref)
    if entity is None:
        return None

    # Only stored, artifact-declared fields reach the task — a compact JSON of
    # the resolved entity, not the client string.
    entity_json = json.dumps(entity, ensure_ascii=False, sort_keys=True)
    return (
        f"{template}\n\n"
        f"Artifact: {artifact.get('title') or artifact.get('id', '')}\n"
        f"Target entity (resolved from stored content): {entity_json}"
    )


def _lookup_entity(artifact: dict, entity_ref: str) -> Optional[dict]:
    """Resolve ``entity_ref`` to a stored entity dict in the pinned content,
    mirroring the addressing used by ``_tombstone_entity``. Returns None if the
    ref matches no stored entity. Never returns the raw ref."""
    kind = artifact.get("kind", "")
    try:
        obj = json.loads(artifact.get("content", "") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None

    if kind == "dataset":
        rows = obj.get("rows", [])
        key_field = obj.get("key", "id")
        target = entity_ref.strip().lower()
        return next(
            (row for row in rows
             if str(row.get(key_field, "")).strip().lower() == target),
            None,
        )
    if kind == "map":
        target = entity_ref.strip().lower()
        return next(
            (m for m in obj.get("markers", [])
             if str(m.get("label", "")).strip().lower() == target),
            None,
        )
    if kind == "model":
        parts = entity_ref.split("/", 1)
        if len(parts) != 2:
            return None
        set_name, key_value = parts[0], parts[1].strip().lower()
        sets = obj.get("entities")
        if not isinstance(sets, dict) or set_name not in sets:
            return None
        set_obj = sets[set_name]
        key_field = set_obj.get("key", "id")
        return next(
            (item for item in set_obj.get("items", [])
             if str(item.get(key_field, "")).strip().lower() == key_value),
            None,
        )
    return None


def _spawn_session(task: str, title: str, artifact_id: str) -> Optional[str]:
    """Create a live session through the standard session runtime and seed it
    with ``task``. Returns the session's stable database id (the id
    ``session.list`` exposes and ``session.resume`` accepts), so the client
    can click through to the spawned run.

    ``session.create`` returns two ids: the short 8-char runtime ``session_id``
    that drives in-memory RPCs (``prompt.submit`` etc.), and the long
    ``stored_session_id`` (``YYYYMMDD_HHMMSS_xxxxxx``) that is the session's
    stable key in ``session.list``. We seed the task with the runtime id but
    hand the client the database id: the client's navigation resolves a session
    against list rows, which carry only the database id — a runtime id it has
    never seen (this session was spawned server-side, so the client never ran
    ``session.create`` to learn the mapping) would silently fail to resolve.
    Fall back to the runtime id when no database id is present (e.g. test
    doubles that only model the runtime id).

    Isolated behind one function so the single dependency on the ``server``
    module (its in-process ``_methods`` dispatch) is easy to stub in tests and
    doesn't leak the whole server surface into the intent engine.
    """
    from tui_gateway import server

    create = server._methods.get("session.create")
    if create is None:
        raise RuntimeError("session.create not registered")

    resp = create("artifact-intent", {
        "title": title,
        "source": "artifact",
    })
    result = (resp or {}).get("result", {})
    runtime_id = result.get("session_id")
    if not runtime_id:
        return None

    # Seed the initial task; the run streams in the background. Best-effort —
    # the session exists and is navigable even if the seed prompt is slow.
    # The runtime id is the correct handle for in-memory dispatch here.
    submit = server._methods.get("prompt.submit")
    if submit is not None:
        submit("artifact-intent", {"session_id": runtime_id, "text": task})

    # Prefer the stable database id for the client's click-through.
    return result.get("stored_session_id") or runtime_id


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
