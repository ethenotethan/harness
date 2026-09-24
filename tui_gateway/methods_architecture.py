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


def _service_param(params: dict):
    service_id = (params or {}).get("service")
    if not isinstance(service_id, str) or not service_id.strip():
        return None
    return service_id.strip()


@method("architecture.list")
def _(rid, params: dict) -> dict:
    """Every manifest-declared service with its source, revision and last check."""
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
                "status": store.status_for(manifest),
            })
        return _ok(rid, {"services": services})
    except Exception as e:
        logger.exception("architecture.list failed")
        return _err(rid, 5040, str(e))


@method("architecture.describe")
def _(rid, params: dict) -> dict:
    """The model for one service: its current revision (read now and snapshotted)
    or a stored ``revision``. A newly seen revision is announced to every client
    as ``architecture.changed``."""
    try:
        from tui_gateway import architecture_store as store

        service_id = _service_param(params)
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

        service_id = _service_param(params)
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
    """Stored revisions (newest last, genesis first) and recorded check runs."""
    try:
        from tui_gateway import architecture_store as store

        service_id = _service_param(params)
        if service_id is None:
            return _err(rid, 4029, "architecture.history needs a 'service' id")
        manifest = store.load_manifest(service_id)
        if manifest is None:
            return _err(rid, 4030, f"unknown service: {service_id}")
        snapshots = store.list_snapshots(manifest["id"])
        return _ok(rid, {
            "service": store.graph_id(manifest),
            "source": store.source_of(manifest),
            "latest": snapshots.get("latest"),
            "revisions": snapshots.get("revisions") or [],
            "checks": store.list_checks(manifest["id"]),
        })
    except Exception as e:
        logger.exception("architecture.history failed")
        return _err(rid, 5043, str(e))


def register(server) -> None:
    _registry.install(server)
