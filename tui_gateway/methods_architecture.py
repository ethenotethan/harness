"""``architecture.*`` — per-service architecture models over JSON-RPC.

A service declared by a manifest under ``~/.hermes/services/architecture/`` has
a compiler-emitted architecture model (system map, invariants, stores,
externals, CI plane). These handlers read it, snapshot it per revision, run the
service's own ``--check`` on demand, list what was recorded, and tail or follow
the log sinks a local service declares — for a local checkout that is never
pushed as much as for a GitHub repository. Portal opens the model from the
service node on the dataflow graph. Contract: docs/api/architecture.md.
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
                "logs": store.resolved_logs(manifest),
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
    """Stored revisions (newest last, genesis first) and recorded check runs."""
    try:
        from tui_gateway import architecture_store as store

        service_id = store.service_param(params)
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


@method("architecture.logs")
def _(rid, params: dict) -> dict:
    """A bounded read of one declared log sink of a local service: without a
    ``cursor`` the last ``lines`` complete lines (default 200, max 2000) and the
    cursor to continue from; with one, every complete line appended since. Only
    the manifest's declared sinks are ever read. **4040** unknown sink (or none
    declared), **4041** the sink has no file yet, **4001** bad params."""
    try:
        from tui_gateway import architecture_logs as logs
        from tui_gateway import architecture_store as store

        service_id = store.service_param(params)
        if service_id is None:
            return _err(rid, 4029, "architecture.logs needs a 'service' id")
        manifest = store.load_manifest(service_id)
        if manifest is None:
            return _err(rid, 4030, f"unknown service: {service_id}")
        sink_id = (params or {}).get("sink")
        sink_id = sink_id.strip() if isinstance(sink_id, str) and sink_id.strip() else None
        try:
            lines = logs.parse_lines((params or {}).get("lines"))
            cursor = logs.parse_cursor((params or {}).get("cursor"))
            result = logs.read_logs(manifest, sink_id, lines, cursor)
        except logs.LogError as exc:
            return _err(rid, exc.code, exc.message)
        result["service"] = store.graph_id(manifest)
        return _ok(rid, result)
    except Exception as e:
        logger.exception("architecture.logs failed")
        return _err(rid, 5044, str(e))


@method("architecture.logs.follow")
def _(rid, params: dict) -> dict:
    """Start or stop following a declared sink. While following, a daemon thread
    polls it every second and broadcasts ``architecture.log`` with the appended
    lines (coalesced, at most 500 per event); one follower per (service, sink);
    it stops on ``enabled: false`` or after ten minutes without a client asking
    again (a final event carries ``stopped``)."""
    try:
        from tui_gateway import architecture_logs as logs
        from tui_gateway import architecture_store as store

        service_id = store.service_param(params)
        if service_id is None:
            return _err(rid, 4029, "architecture.logs.follow needs a 'service' id")
        manifest = store.load_manifest(service_id)
        if manifest is None:
            return _err(rid, 4030, f"unknown service: {service_id}")
        enabled = (params or {}).get("enabled", True)
        if not isinstance(enabled, bool):
            return _err(rid, 4001, "enabled must be a boolean")
        sink_id = (params or {}).get("sink")
        sink_id = sink_id.strip() if isinstance(sink_id, str) and sink_id.strip() else None
        service = store.graph_id(manifest)
        try:
            sink = logs.select_sink(manifest, sink_id)
            if not enabled:
                stopped = logs.stop_follow(service, sink["id"])
                return _ok(rid, {"service": service, "following": False, "sink": sink["id"], "stopped": stopped})
            cursor = logs.parse_cursor((params or {}).get("cursor"))
            follower = logs.start_follow(manifest, service, sink["id"], _broadcast_global_event, cursor=cursor)
        except logs.LogError as exc:
            return _err(rid, exc.code, exc.message)
        return _ok(rid, {"service": service, "following": True, "sink": follower.sink["id"], "cursor": str(follower.cursor)})
    except Exception as e:
        logger.exception("architecture.logs.follow failed")
        return _err(rid, 5045, str(e))


def register(server) -> None:
    _registry.install(server)
