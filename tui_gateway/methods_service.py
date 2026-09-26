"""``service.*`` — what a running service exposes beyond its dataflow node.

Today: its logs. ``service.logs`` tails or continues one of a service's log
sinks and ``service.logs.follow`` streams appended lines as ``service.log``
events. The ``service`` param is a graph service id (``arch:<id>``,
``launchd:<label>``, ``docker:<12>``, ``nomad:<job>``, ``proc_<id>``); sinks
resolve per provider in ``tui_gateway/service_logs.py``, and only those paths
are ever read. Docs: docs/api/service-logs.md.
"""
from __future__ import annotations

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method

# No module-level helpers below: ``HandlerRegistry.install`` rebinds each handler's
# globals to the server module, so anything a body needs must be imported inside
# it or be a server global (``_ok``, ``_err``, ``_broadcast_global_event``).


@method("service.logs")
def _(rid, params: dict) -> dict:
    """A bounded read of one log sink of a service: without a ``cursor`` the last
    ``lines`` complete lines (default 200, max 2000) and the cursor to continue
    from; with one, every complete line appended since. Only the sinks the
    service resolves to are ever read. **4040** unknown sink (or none), **4041**
    the sink has no file yet, **4042** provider without log capture, **4001** bad params."""
    try:
        from tui_gateway import service_logs as logs

        service = (params or {}).get("service") if isinstance(params, dict) else None
        if not isinstance(service, str) or not service.strip():
            return _err(rid, 4029, "service.logs needs a 'service' graph id")
        service = service.strip()
        sink_id = (params or {}).get("sink")
        sink_id = sink_id.strip() if isinstance(sink_id, str) and sink_id.strip() else None
        try:
            lines = logs.parse_lines((params or {}).get("lines"))
            cursor = logs.parse_cursor((params or {}).get("cursor"))
            sinks = logs.service_sinks(service)
            result = logs.read_logs(sinks, service, sink_id, lines, cursor)
        except logs.LogError as exc:
            return _err(rid, exc.code, exc.message)
        return _ok(rid, result)
    except Exception as e:
        logger.exception("service.logs failed")
        return _err(rid, 5044, str(e))


@method("service.logs.follow")
def _(rid, params: dict) -> dict:
    """Start or stop following a sink. While following, a daemon thread polls it
    every second and broadcasts ``service.log`` with the appended lines
    (coalesced, at most 500 per event); one follower per (service, sink); it
    stops on ``enabled: false`` or after ten minutes without a client asking
    again (a final event carries ``stopped``)."""
    try:
        from tui_gateway import service_logs as logs

        service = (params or {}).get("service") if isinstance(params, dict) else None
        if not isinstance(service, str) or not service.strip():
            return _err(rid, 4029, "service.logs.follow needs a 'service' graph id")
        service = service.strip()
        enabled = (params or {}).get("enabled", True)
        if not isinstance(enabled, bool):
            return _err(rid, 4001, "enabled must be a boolean")
        sink_id = (params or {}).get("sink")
        sink_id = sink_id.strip() if isinstance(sink_id, str) and sink_id.strip() else None
        try:
            sinks = logs.service_sinks(service)
            sink = logs.select_sink(sinks, sink_id, service)
            if not enabled:
                stopped = logs.stop_follow(service, sink["id"])
                return _ok(rid, {"service": service, "following": False, "sink": sink["id"], "stopped": stopped})
            cursor = logs.parse_cursor((params or {}).get("cursor"))
            follower = logs.start_follow(sinks, service, sink["id"], _broadcast_global_event, cursor=cursor)
        except logs.LogError as exc:
            return _err(rid, exc.code, exc.message)
        return _ok(rid, {"service": service, "following": True, "sink": follower.sink["id"], "cursor": str(follower.cursor)})
    except Exception as e:
        logger.exception("service.logs.follow failed")
        return _err(rid, 5045, str(e))


def register(server) -> None:
    _registry.install(server)
