"""
Artifact backend queries — the read side of artifact intents.

Living artifacts can *command* the gateway through intents
(``artifact_actions``). This module lets them *read* from it: an HTML
dashboard asks for "open orders, state=open, 100 rows" and gets JSON back,
continuously, from a database or service the gateway holds the credentials
for. The shape is the one every full-stack app already uses across a service
boundary — the browser never sends SQL. It calls a named endpoint with typed
variables; the API layer validates, executes, and pushes results back.

Three parties, three responsibilities
-------------------------------------
* **The query plugin** (``~/.hermes/plugins/actions/*.py``) owns the
  statement and the connection. It registers a *named* handler —
  ``postgres.orders.open`` — with a parameter schema. This is the persisted
  query / allow-list pattern: adding a query means adding a statement to the
  plugin directory, which only a human can write to.
* **The artifact** declares which handlers its page may call (``queries``
  on the artifact record, pinned to the revision like ``actions``) and may
  narrow their parameters or bind them to constants. It supplies parameter
  *shapes and values*, never query text.
* **The page** supplies parameter values through inert ``data-hermes-query``
  / ``data-hermes-params`` attributes. It can change *variables*; it cannot
  change *what runs*.

Security invariants
-------------------
* The handler is resolved server-side from the artifact's revision-pinned
  ``queries`` declaration. The client sends a ``query_id``, never a handler
  name; a forged ``query_id`` is ``unsupported``.
* Every parameter is validated against the artifact's declared schema AND
  the handler's registered schema before the handler runs. Unknown keys are
  rejected, not ignored. Bound (``bind``) values cannot be overridden.
* Results are JSON data, size-capped. The gateway never returns markup and
  the native client never evaluates what comes back.
* Read only. A handler that mutates belongs in ``artifact_actions`` where
  the confirmation flow lives.
* Rate-limited per (artifact, query): a page in a re-render loop cannot
  hammer a database.

Continuity
----------
``subscribe`` registers interest in a (query, params) slot. The gateway
re-runs subscribed queries itself — on the declared ``live`` cadence, or
when a plugin calls ``mark_changed`` from a webhook / LISTEN thread — and
emits ``artifact.query.changed`` **only when the result's etag differs**.
Polling therefore happens where the credentials live, and the client never
polls: it re-fetches on the event. Slots are dropped when the last
subscriber leaves.
"""

import hashlib
import json
import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────

MAX_PARAMS = 16
MAX_PARAM_STRING = 1_024
MAX_CURSOR = 512
MAX_RESULT_BYTES = 256_000
MAX_ROWS = 1_000
DEFAULT_ROWS = 100

# Per (artifact, query) slot: `RATE_LIMIT_CALLS` invocations per `RATE_LIMIT_WINDOW`.
RATE_LIMIT_CALLS = 30
RATE_LIMIT_WINDOW = 10.0  # seconds

MIN_POLL_INTERVAL = 5.0
MAX_POLL_INTERVAL = 3_600.0
DEFAULT_POLL_INTERVAL = 30.0

_PARAM_TYPES = {"string", "int", "number", "bool", "enum", "cursor"}


class QueryError(Exception):
    """A request the gateway refuses before any handler runs."""


# ── Handler registry ─────────────────────────────────────────────────────────

# name -> {"fn": callable(artifact_id, query_id, params, cursor) -> dict,
#          "params": schema dict | None}
_QUERY_HANDLERS: dict[str, dict[str, Any]] = {}


def register_query_handler(name: str, fn: Callable, params: Optional[dict] = None) -> None:
    """Register a read handler.

    ``fn(artifact_id, query_id, params, cursor)`` returns ``{"data": <json>,
    "next_cursor": <str|None>}`` or raises. ``params`` is the handler's own
    parameter schema (see :func:`validate_params`); it is authoritative —
    an artifact may narrow it or bind constants, never widen it.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("query handler name required")
    if params is not None and not isinstance(params, dict):
        raise ValueError("query handler params schema must be a dict")
    _QUERY_HANDLERS[name] = {"fn": fn, "params": params}


def _query_handler(name: str, params: Optional[dict] = None):
    def decorator(fn):
        register_query_handler(name, fn, params)
        return fn
    return decorator


def registered_query_names() -> list[str]:
    return sorted(_QUERY_HANDLERS)


# ── Parameter validation ─────────────────────────────────────────────────────


def validate_params(schema: Optional[dict], supplied: Optional[dict]) -> dict:
    """Validate and coerce ``supplied`` against ``schema``.

    Schema shape (per parameter)::

        {"type": "string", "max": 80, "required": true, "default": "x"}
        {"type": "int", "min": 1, "max": 500, "default": 100}
        {"type": "number", "min": 0}
        {"type": "bool"}
        {"type": "enum", "values": ["open", "closed"]}
        {"type": "cursor"}

    Unknown keys are an error: a query that silently ignored a parameter
    would answer a different question than the one the page asked. A
    ``None`` schema accepts only an empty parameter set — a handler with no
    declared parameters takes none.
    """
    supplied = dict(supplied or {})
    if len(supplied) > MAX_PARAMS:
        raise QueryError(f"too many parameters ({len(supplied)} > {MAX_PARAMS})")
    schema = schema or {}
    unknown = sorted(set(supplied) - set(schema))
    if unknown:
        raise QueryError(f"unknown parameter(s): {', '.join(unknown)}")

    out: dict[str, Any] = {}
    for name, spec in schema.items():
        if not isinstance(spec, dict):
            raise QueryError(f"parameter {name!r}: schema entry must be an object")
        ptype = spec.get("type", "string")
        if ptype not in _PARAM_TYPES:
            raise QueryError(f"parameter {name!r}: unknown type {ptype!r}")
        if name not in supplied or supplied[name] is None:
            if "default" in spec:
                out[name] = spec["default"]
            elif spec.get("required"):
                raise QueryError(f"parameter {name!r} is required")
            continue
        out[name] = _coerce(name, ptype, spec, supplied[name])
    return out


def _coerce(name: str, ptype: str, spec: dict, value: Any) -> Any:
    if ptype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
            return value.strip().lower() in {"true", "1"}
        raise QueryError(f"parameter {name!r}: expected a boolean")

    if ptype == "int":
        # bool is an int subclass in Python; a page sending true for a count
        # is a bug, not a 1.
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise QueryError(f"parameter {name!r}: expected an integer")
        try:
            number = int(value)
        except ValueError:
            raise QueryError(f"parameter {name!r}: expected an integer") from None
        return _bounded(name, number, spec)

    if ptype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise QueryError(f"parameter {name!r}: expected a number")
        try:
            number = float(value)
        except ValueError:
            raise QueryError(f"parameter {name!r}: expected a number") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise QueryError(f"parameter {name!r}: expected a finite number")
        return _bounded(name, number, spec)

    if not isinstance(value, str):
        raise QueryError(f"parameter {name!r}: expected a string")
    if any(ord(ch) < 32 and ch not in "\t\n" for ch in value):
        raise QueryError(f"parameter {name!r}: control characters are not allowed")

    if ptype == "enum":
        values = spec.get("values")
        if not isinstance(values, list) or not values:
            raise QueryError(f"parameter {name!r}: enum declares no values")
        if value not in values:
            raise QueryError(f"parameter {name!r}: must be one of {values}")
        return value

    if ptype == "cursor":
        if len(value.encode("utf-8")) > MAX_CURSOR:
            raise QueryError(f"parameter {name!r}: cursor too long")
        return value

    # string
    limit = spec.get("max", MAX_PARAM_STRING)
    if not isinstance(limit, int) or limit <= 0 or limit > MAX_PARAM_STRING:
        limit = MAX_PARAM_STRING
    if len(value) > limit:
        raise QueryError(f"parameter {name!r}: longer than {limit} characters")
    return value


def _bounded(name: str, number, spec: dict):
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and number < lo:
        raise QueryError(f"parameter {name!r}: below minimum {lo}")
    if hi is not None and number > hi:
        raise QueryError(f"parameter {name!r}: above maximum {hi}")
    return number


# ── Declaration resolution ───────────────────────────────────────────────────


def _resolve_query(artifact: dict, query_id: str) -> Optional[dict]:
    """The artifact's declaration for ``query_id`` at its stored revision."""
    for decl in artifact.get("queries") or []:
        if isinstance(decl, dict) and decl.get("id") == query_id:
            return decl
    return None


def validate_declarations(queries: Any) -> list[dict]:
    """Shape-check a ``queries`` manifest at write time.

    Only shape: the handler need not exist yet (plugins reload), and the
    page's parameters are checked at invoke time. What has to be right now is
    that every declaration names an id and a handler, because a declaration
    missing either can never resolve and would dead-button the page with no
    signal to whoever wrote it.
    """
    if not isinstance(queries, list):
        raise ValueError("queries must be a list of query declarations")
    seen: set[str] = set()
    out: list[dict] = []
    for decl in queries:
        if not isinstance(decl, dict):
            raise ValueError("each query declaration must be an object")
        qid = str(decl.get("id", "")).strip()
        handler = str(decl.get("query", "")).strip()
        if not qid or not handler:
            raise ValueError("query declarations need both an id and a query (handler name)")
        if qid in seen:
            raise ValueError(f"duplicate query id {qid!r}")
        seen.add(qid)
        params = decl.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError(f"query {qid!r}: params must be an object")
        bind = decl.get("bind")
        if bind is not None and not isinstance(bind, dict):
            raise ValueError(f"query {qid!r}: bind must be an object")
        live = decl.get("live")
        if live is not None and not isinstance(live, dict):
            raise ValueError(f"query {qid!r}: live must be an object")
        invalidated_by = decl.get("invalidated_by")
        if invalidated_by is not None and not (
            isinstance(invalidated_by, list) and all(isinstance(b, str) for b in invalidated_by)
        ):
            raise ValueError(f"query {qid!r}: invalidated_by must be a list of binding ids")
        out.append(decl)
    return out


def _effective_params(decl: dict, handler: dict, supplied: Optional[dict]) -> dict:
    """What the handler will see: the page's values through the artifact's
    schema, the author's bound values on top, and the whole through the
    handler's schema.

    The artifact's ``params`` describes only what the *page* may vary, so
    bound keys are not checked against it — they are the author's, and the
    handler's schema is what validates them. A page that sends a bound key
    with a different value is refused rather than silently corrected.
    """
    supplied = dict(supplied or {})
    bind = decl.get("bind") or {}
    for key, value in bind.items():
        if key in supplied and supplied[key] != value:
            raise QueryError(f"parameter {key!r} is bound by the artifact and cannot be overridden")
        supplied.pop(key, None)

    declared_schema = decl.get("params")
    handler_schema = handler.get("params")
    if declared_schema is not None:
        supplied = validate_params(declared_schema, supplied)
    elif supplied and handler_schema is None:
        raise QueryError("this query takes no parameters")

    supplied.update(bind)
    if handler_schema is not None:
        return validate_params(handler_schema, supplied)
    return supplied


# ── Rate limiting ────────────────────────────────────────────────────────────

_rate_lock = threading.Lock()
_rate_windows: dict[str, list[float]] = {}


def _rate_limited(slot: str, now: Optional[float] = None) -> bool:
    now = time.monotonic() if now is None else now
    with _rate_lock:
        window = [t for t in _rate_windows.get(slot, []) if now - t < RATE_LIMIT_WINDOW]
        if len(window) >= RATE_LIMIT_CALLS:
            _rate_windows[slot] = window
            return True
        window.append(now)
        _rate_windows[slot] = window
        return False


# ── Invocation ───────────────────────────────────────────────────────────────


def _etag(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def params_hash(params: Optional[dict]) -> str:
    return _etag(params or {})


def invoke(
    artifact_id: str,
    artifact_rev: Optional[int],
    query_id: str,
    params: Optional[dict] = None,
    cursor: Optional[str] = None,
    actor: str = "",
    _skip_rate_limit: bool = False,
) -> dict:
    """Resolve and run a declared query.

    Returns ``status`` in:
    ``ok`` — ``data`` (JSON), ``etag``, ``params`` (as validated),
        ``next_cursor`` when the handler paginates.
    ``failed`` — ``reason``: bad parameters, handler error, oversize result,
        rate limit. Parameter problems are the page's bug and say so.
    ``conflict`` — the artifact changed since the page rendered; the client
        refreshes and re-issues with the new revision. ``artifact_rev=None``
        skips the check (the subscription poller, which tracks the live
        declaration rather than a rendered one).
    ``unsupported`` — no such declaration, or its handler isn't registered.
    """
    from tui_gateway import artifact_store

    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        return {"status": "failed", "reason": f"artifact not found: {artifact_id!r}"}
    if artifact_rev is not None and artifact.get("rev", 0) != artifact_rev:
        return {"status": "conflict"}

    decl = _resolve_query(artifact, query_id)
    if decl is None:
        return {"status": "unsupported", "reason": f"artifact declares no query {query_id!r}"}
    handler_name = str(decl.get("query", ""))
    handler = _QUERY_HANDLERS.get(handler_name)
    if handler is None:
        return {
            "status": "unsupported",
            "reason": f"no query handler registered as {handler_name!r} — is its plugin loaded?",
        }

    slot = f"{artifact_id}/{query_id}"
    if not _skip_rate_limit and _rate_limited(slot):
        return {"status": "failed", "reason": "rate limited — this query is being re-run too often"}

    try:
        effective = _effective_params(decl, handler, params)
    except QueryError as exc:
        return {"status": "failed", "reason": str(exc)}

    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor.encode("utf-8")) > MAX_CURSOR:
            return {"status": "failed", "reason": "invalid cursor"}
        cursor = cursor or None

    t0 = time.monotonic()
    try:
        raw = handler["fn"](
            artifact_id=artifact_id, query_id=query_id, params=effective, cursor=cursor
        )
    except QueryError as exc:
        return {"status": "failed", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a plugin's failure is the page's failure, reported
        logger.warning("query handler %s failed: %s", handler_name, exc)
        return {"status": "failed", "reason": f"query failed: {exc}"}
    duration_ms = int((time.monotonic() - t0) * 1000)

    if not isinstance(raw, dict) or "data" not in raw:
        return {"status": "failed", "reason": "handler returned no data"}
    data = raw["data"]
    try:
        encoded = json.dumps(data, default=str)
    except (TypeError, ValueError) as exc:
        return {"status": "failed", "reason": f"handler returned non-JSON data: {exc}"}
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        return {
            "status": "failed",
            "reason": f"result exceeds {MAX_RESULT_BYTES} bytes — page the query or narrow it",
        }
    # Round-trip so the handler's Decimals / dates land as the strings the
    # client will see; the etag has to be over exactly that.
    data = json.loads(encoded)

    result: dict[str, Any] = {
        "status": "ok",
        "data": data,
        "etag": _etag(data),
        "params": effective,
        "duration_ms": duration_ms,
    }
    next_cursor = raw.get("next_cursor")
    if isinstance(next_cursor, str) and next_cursor:
        result["next_cursor"] = next_cursor
    return result


# ── Subscriptions ────────────────────────────────────────────────────────────

_subs_lock = threading.Lock()
# key -> subscription record
_SUBSCRIPTIONS: dict[str, dict[str, Any]] = {}
_emitter: Optional[Callable[[str, dict], None]] = None
_poller: Optional[threading.Thread] = None
_poller_wake = threading.Event()
_poller_stop = threading.Event()


def set_emitter(fn: Optional[Callable[[str, dict], None]]) -> None:
    """Install the function that broadcasts ``artifact.query.changed``.

    ``fn(event_name, payload)``. The gateway wires this to its ``_emit``; tests
    install a list-appender.
    """
    global _emitter
    _emitter = fn


def _live_interval(decl: dict) -> Optional[float]:
    """Seconds between polls, or None for push-only (``mark_changed``)."""
    live = decl.get("live") or {}
    mode = str(live.get("mode", "poll" if live else "off")).lower()
    if mode == "off":
        return None
    if mode == "subscribe":
        return None
    interval = live.get("interval_s", DEFAULT_POLL_INTERVAL)
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, interval))


def _is_live(decl: dict) -> bool:
    live = decl.get("live") or {}
    return bool(live) and str(live.get("mode", "poll")).lower() != "off"


def subscription_key(artifact_id: str, query_id: str, params: Optional[dict]) -> str:
    return f"{artifact_id}/{query_id}/{params_hash(params)}"


def subscribe(
    artifact_id: str,
    artifact_rev: Optional[int],
    query_id: str,
    params: Optional[dict] = None,
) -> dict:
    """Register interest in a (query, params) slot; returns the current result.

    The first call runs the query so the subscriber has a baseline etag; the
    poller then re-runs on the declared cadence and emits only on change.
    """
    from tui_gateway import artifact_store

    artifact = artifact_store.get_artifact(artifact_id)
    if artifact is None:
        return {"status": "failed", "reason": f"artifact not found: {artifact_id!r}"}
    if artifact_rev is not None and artifact.get("rev", 0) != artifact_rev:
        return {"status": "conflict"}
    decl = _resolve_query(artifact, query_id)
    if decl is None:
        return {"status": "unsupported", "reason": f"artifact declares no query {query_id!r}"}
    if not _is_live(decl):
        return {"status": "unsupported", "reason": f"query {query_id!r} declares no live mode"}

    first = invoke(artifact_id, None, query_id, params, _skip_rate_limit=True)
    if first.get("status") != "ok":
        return first

    key = subscription_key(artifact_id, query_id, first["params"])
    interval = _live_interval(decl)
    now = time.monotonic()
    with _subs_lock:
        record = _SUBSCRIPTIONS.get(key)
        if record is None:
            record = {
                "key": key,
                "artifact_id": artifact_id,
                "query_id": query_id,
                "handler": str(decl.get("query", "")),
                "params": first["params"],
                "etag": first["etag"],
                "interval": interval,
                "next_due": (now + interval) if interval else None,
                "subscribers": 0,
            }
            _SUBSCRIPTIONS[key] = record
        record["subscribers"] += 1
        record["etag"] = first["etag"]
    _ensure_poller()
    result = dict(first)
    result["subscription"] = key
    result["interval_s"] = interval
    return result


def unsubscribe(key: str) -> dict:
    with _subs_lock:
        record = _SUBSCRIPTIONS.get(key)
        if record is None:
            return {"status": "ok", "removed": False}
        record["subscribers"] -= 1
        if record["subscribers"] <= 0:
            del _SUBSCRIPTIONS[key]
            return {"status": "ok", "removed": True}
    return {"status": "ok", "removed": False}


def active_subscriptions() -> list[dict]:
    with _subs_lock:
        return [dict(r) for r in _SUBSCRIPTIONS.values()]


def mark_changed(handler: Optional[str] = None, artifact_id: Optional[str] = None) -> int:
    """A plugin's way of saying "the data behind these queries moved".

    Marks every matching subscription due now — by handler name (a
    Postgres LISTEN thread: everything under ``postgres.``), by artifact, or
    all of them — and wakes the poller. Returns how many were marked. The
    poller still compares etags, so a change that produced the same rows
    emits nothing.
    """
    prefix = (handler or "").strip()
    marked = 0
    with _subs_lock:
        for record in _SUBSCRIPTIONS.values():
            if prefix and not (record["handler"] == prefix or record["handler"].startswith(prefix + ".")):
                continue
            if artifact_id and record["artifact_id"] != artifact_id:
                continue
            record["next_due"] = 0.0
            marked += 1
    if marked:
        _poller_wake.set()
    return marked


def run_due(now: Optional[float] = None) -> list[dict]:
    """Re-run every subscription that is due; emit for those whose data changed.

    Public so tests (and a gateway without threads) can drive it directly.
    Returns the emitted payloads.
    """
    now = time.monotonic() if now is None else now
    with _subs_lock:
        due = [dict(r) for r in _SUBSCRIPTIONS.values()
               if r["next_due"] is not None and r["next_due"] <= now]
    emitted: list[dict] = []
    for record in due:
        result = invoke(
            record["artifact_id"], None, record["query_id"], record["params"],
            _skip_rate_limit=True,
        )
        status = result.get("status")
        with _subs_lock:
            live = _SUBSCRIPTIONS.get(record["key"])
            if live is None:
                continue
            live["next_due"] = (now + live["interval"]) if live["interval"] else None
            if status == "ok":
                changed = result["etag"] != live["etag"]
                live["etag"] = result["etag"]
                payload = None
                if changed:
                    payload = {
                        "artifact_id": record["artifact_id"],
                        "query_id": record["query_id"],
                        "params_hash": params_hash(record["params"]),
                        "etag": result["etag"],
                        "status": "ok",
                    }
            elif status in ("unsupported",):
                # The declaration or its handler went away: tell the page once,
                # then stop watching a query that can no longer answer.
                del _SUBSCRIPTIONS[record["key"]]
                payload = {
                    "artifact_id": record["artifact_id"],
                    "query_id": record["query_id"],
                    "params_hash": params_hash(record["params"]),
                    "status": "unsupported",
                    "reason": result.get("reason", ""),
                }
            else:
                # Transient failure: keep the slot, say nothing, try next tick.
                payload = None
        if payload:
            emitted.append(payload)
            if _emitter is not None:
                try:
                    _emitter("artifact.query.changed", payload)
                except Exception:  # noqa: BLE001
                    logger.exception("artifact.query.changed emit failed")
    return emitted


def _poll_loop() -> None:
    while not _poller_stop.is_set():
        try:
            run_due()
        except Exception:  # noqa: BLE001
            logger.exception("artifact query poller tick failed")
        _poller_wake.wait(1.0)
        _poller_wake.clear()


def _ensure_poller() -> None:
    global _poller
    if _poller is not None and _poller.is_alive():
        return
    _poller_stop.clear()
    _poller = threading.Thread(target=_poll_loop, name="artifact-query-poller", daemon=True)
    _poller.start()


def stop_poller() -> None:
    """Tests and shutdown."""
    _poller_stop.set()
    _poller_wake.set()


def reset_for_tests() -> None:
    stop_poller()
    with _subs_lock:
        _SUBSCRIPTIONS.clear()
    with _rate_lock:
        _rate_windows.clear()


# ── Built-in handler: rows of another artifact ───────────────────────────────

_ROWS_SCHEMA = {
    "source": {"type": "string", "max": 128, "required": True},
    "set": {"type": "string", "max": 128},
    "limit": {"type": "int", "min": 1, "max": MAX_ROWS, "default": DEFAULT_ROWS},
}


@_query_handler("artifact.rows", _ROWS_SCHEMA)
def _handle_rows(artifact_id: str, query_id: str, params: dict, cursor: Optional[str]) -> dict:
    """The entries of a dataset / map / checklist / kanban / calendar / model
    artifact, paginated by offset — an HTML dashboard over the artifacts the
    agent already maintains, with no database at all.

    Tombstoned entries are omitted, the same way every renderer omits them.
    """
    from tui_gateway import artifact_store

    source = artifact_store.get_artifact(params["source"])
    if source is None:
        raise QueryError(f"source artifact not found: {params['source']!r}")
    try:
        content = json.loads(source.get("content") or "{}")
    except ValueError:
        raise QueryError("source artifact content is not JSON") from None
    if not isinstance(content, dict):
        raise QueryError("source artifact content is not an object")

    kind = source.get("kind", "")
    if kind == "model":
        sets = content.get("entities") or {}
        set_name = params.get("set")
        if not set_name:
            raise QueryError("model artifacts need a `set` parameter")
        entity_set = sets.get(set_name)
        if not isinstance(entity_set, dict):
            raise QueryError(f"no entity set {set_name!r} in source artifact")
        entries = entity_set.get("items") or []
    else:
        list_field = {
            "dataset": "rows", "map": "markers", "checklist": "items",
            "kanban": "cards", "calendar": "events",
        }.get(kind)
        if list_field is None:
            raise QueryError(f"artifact.rows does not read {kind!r} artifacts")
        entries = content.get(list_field) or []

    entries = [e for e in entries if isinstance(e, dict) and not e.get("_deleted")]
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except ValueError:
            raise QueryError("invalid cursor") from None
    limit = params["limit"]
    page = entries[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(entries) else None
    return {
        "data": {"rows": page, "total": len(entries), "rev": source.get("rev", 0)},
        "next_cursor": next_cursor,
    }
