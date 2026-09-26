"""``architecture.*`` — per-service architecture models over JSON-RPC.

A service declared by a manifest under ``~/.hermes/services/architecture/`` has
a compiler-emitted architecture model (system map, invariants, stores,
externals, CI plane). These handlers read it, snapshot it per revision, run the
service's own ``--check`` on demand, and list what was recorded — for a local
checkout that is never pushed as much as for a GitHub repository. Portal opens
the model from the service node on the dataflow graph. Contract: docs/api/architecture.md.
"""
from __future__ import annotations

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method

# No module-level helpers below: ``HandlerRegistry.install`` rebinds each handler's
# globals to the server module, so anything a body needs must be imported inside
# it (``architecture_store.service_param``) or be a server global (``_ok``).


@method("architecture.list")
def _(rid, params: dict) -> dict:
    """Every manifest-declared service with its source, revision, last check and
    whether its model conforms to the hermes.architecture contract."""
    try:
        from tui_gateway import architecture_store as store

        services = []
        for manifest in store.list_manifests():
            services.append({
                "id": store.graph_id(manifest),
                "label": manifest["name"],
                "description": manifest["description"],
                "source": store.source_of(manifest),
                "root": manifest.get("root"),
                "repository": manifest.get("repository"),
                "ref": manifest.get("ref") if manifest.get("repository") else None,
                "model": manifest["model"],
                "check_configured": bool(manifest.get("check")),
                "runtime": manifest.get("runtime"),
                "status": store.status_for(manifest),
            })
        # status carries each service's contract + conforming; the list names the
        # contract this gateway validates against so a client can feature-gate once.
        return _ok(rid, {"services": services, "contract": store.contract_ref()})
    except Exception as e:
        logger.exception("architecture.list failed")
        return _err(rid, 5040, str(e))


@method("architecture.describe")
def _(rid, params: dict) -> dict:
    """The model for one service: its current revision (read now, validated
    against the contract and snapshotted) or a stored ``revision``. A document
    that does not conform is refused with 4033 and the message names the first
    problems. A newly seen revision is announced as ``architecture.changed``."""
    try:
        from tui_gateway import architecture_store as store

        service_id = store.service_param(params)
        if service_id is None:
            return _err(rid, 4029, "architecture.describe needs a 'service' id")
        manifest = store.load_manifest(service_id)
        if manifest is None:
            return _err(rid, 4030, f"unknown service: {service_id}")
        revision = (params or {}).get("revision")
        revision = revision.strip() if isinstance(revision, str) and revision.strip() else None
        before = store.list_snapshots(manifest["id"]).get("latest")
        try:
            document = store.describe(manifest, revision=revision)
        except store.ArchitectureError as exc:
            return _err(rid, exc.code, exc.message)
        if revision is None and document["revision"] != before:
            _broadcast_global_event("architecture.changed", {
                "service": document["service"]["id"],
                "revision": document["revision"],
                "source": document["source"],
                "reason": "snapshot",
            })
        return _ok(rid, document)
    except Exception as e:
        logger.exception("architecture.describe failed")
        return _err(rid, 5041, str(e))


@method("architecture.check")
def _(rid, params: dict) -> dict:
    """Run the service's own ``--check`` in its root and record the result. Local
    services only; a GitHub service reports ``unavailable`` because its checks run
    in its repository's CI. The outcome is announced as ``architecture.changed``."""
    try:
        from tui_gateway import architecture_store as store

        service_id = store.service_param(params)
        if service_id is None:
            return _err(rid, 4029, "architecture.check needs a 'service' id")
        manifest = store.load_manifest(service_id)
        if manifest is None:
            return _err(rid, 4030, f"unknown service: {service_id}")
        result = store.run_check(manifest)
        # Only a check that ran is news; "unavailable" changes nothing a client shows.
        if result.get("status") in ("passed", "failed"):
            _broadcast_global_event("architecture.changed", {
                "service": store.graph_id(manifest),
                "revision": result.get("revision"),
                "source": store.source_of(manifest),
                "reason": "check",
                "status": result.get("status"),
            })
        return _ok(rid, {"service": store.graph_id(manifest), "check": result})
    except Exception as e:
        logger.exception("architecture.check failed")
        return _err(rid, 5042, str(e))


@method("architecture.history")
def _(rid, params: dict) -> dict:
    """Stored revisions (genesis first, newest last) with the commit behind each
    and the commits since the previous snapshot (local roots), which revision the
    checkout is at now (``deployed``), the runtime provider's view when bound,
    and recorded check runs."""
    try:
        from tui_gateway import architecture_store as store

        service_id = store.service_param(params)
        if service_id is None:
            return _err(rid, 4029, "architecture.history needs a 'service' id")
        manifest = store.load_manifest(service_id)
        if manifest is None:
            return _err(rid, 4030, f"unknown service: {service_id}")
        return _ok(rid, store.history(manifest))
    except Exception as e:
        logger.exception("architecture.history failed")
        return _err(rid, 5043, str(e))


@method("architecture.diff")
def _(rid, params: dict) -> dict:
    """What changed between two stored revisions: constructions, edges,
    invariants, files, gates (by stable identity) and, for a local root, the
    commits and per-file line counts between them. ``to`` defaults to the latest
    snapshot, ``from`` to the one before it."""
    try:
        from tui_gateway import architecture_store as store

        service_id = store.service_param(params)
        if service_id is None:
            return _err(rid, 4029, "architecture.diff needs a 'service' id")
        manifest = store.load_manifest(service_id)
        if manifest is None:
            return _err(rid, 4030, f"unknown service: {service_id}")
        params = params or {}
        ends = {}
        for key in ("from", "to"):
            value = params.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return _err(rid, 4001, f"architecture.diff '{key}' must be a non-empty revision string")
            ends[key] = value.strip() if isinstance(value, str) else None
        try:
            return _ok(rid, store.diff(manifest, ends["from"], ends["to"]))
        except store.ArchitectureError as exc:
            return _err(rid, exc.code, exc.message)
    except Exception as e:
        logger.exception("architecture.diff failed")
        return _err(rid, 5044, str(e))


def register(server) -> None:
    _registry.install(server)
