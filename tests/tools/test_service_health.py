"""Health-gated service leases and graph health evidence."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
import json
import socket
import sys

import pytest


def test_http_health_spec_normalizes_defaults():
    from tools.service_health import normalize_service_health

    assert normalize_service_health({
        "type": "http",
        "url": " http://127.0.0.1:9120/health ",
    }) == {
        "type": "http",
        "url": "http://127.0.0.1:9120/health",
        "expected_status": 200,
        "timeout_seconds": 2.0,
        "startup_timeout_seconds": 30.0,
    }


def test_http_health_spec_rejects_non_http_target():
    from tools.service_health import normalize_service_health

    with pytest.raises(ValueError, match="http or https URL"):
        normalize_service_health({"type": "http", "url": "file:/tmp/ready"})


def test_http_probe_returns_graph_ready_health_evidence():
    from tools.service_health import normalize_service_health, probe_service_health

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        spec = normalize_service_health({
            "type": "http",
            "url": f"http://127.0.0.1:{server.server_port}/health",
        })
        evidence = probe_service_health(spec)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert evidence["status"] == "healthy"
    assert evidence["probe"] == "http"
    assert evidence["target"] == spec["url"]
    assert evidence["checked_at"].endswith("Z")
    assert evidence["latency_ms"] >= 0
    assert evidence["message"] == "HTTP 200"


def test_wait_for_health_returns_last_failure_at_deadline():
    from tools.service_health import normalize_service_health, wait_for_service_health

    spec = normalize_service_health({
        "type": "http",
        "url": "http://127.0.0.1:1/health",
        "timeout_seconds": 0.1,
        "startup_timeout_seconds": 0.1,
    })
    evidence = wait_for_service_health(spec, retry_interval_seconds=0.01)

    assert evidence["status"] == "unhealthy"
    assert evidence["target"] == spec["url"]


def test_terminal_rolls_back_process_when_readiness_never_passes(monkeypatch, tmp_path):
    import tools.terminal_tool as terminal_tool
    from tools import process_registry as process_registry_module
    from tools import service_health as service_health_module

    class FakeRegistry:
        pending_watchers = []

        def __init__(self):
            self.spawned = None
            self.killed = []
            self.committed = []

        def spawn_local(self, **kwargs):
            self.spawned = kwargs
            return SimpleNamespace(id="proc_health", pid=4242)

        def kill_process(self, session_id, **_kwargs):
            self.killed.append(session_id)
            return {"status": "killed"}

        def commit_service_lease(self, session_id, evidence):
            self.committed.append((session_id, evidence))

    registry = FakeRegistry()
    config = {
        "env_type": "local", "docker_image": "", "singularity_image": "",
        "modal_image": "", "daytona_image": "", "cwd": str(tmp_path), "timeout": 30,
    }
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True}
    )
    monkeypatch.setattr(process_registry_module, "process_registry", registry)
    monkeypatch.setattr(
        service_health_module,
        "wait_for_service_health",
        lambda _spec: {
            "status": "unhealthy", "probe": "http", "target": "http://127.0.0.1:1/health",
            "checked_at": "2026-08-23T22:00:00Z", "latency_ms": 1, "message": "refused",
        },
    )
    monkeypatch.setitem(terminal_tool._active_environments, "default", SimpleNamespace(env={}))
    monkeypatch.setitem(terminal_tool._last_activity, "default", 0.0)
    try:
        result = json.loads(terminal_tool.terminal_tool(
            command="python api.py", background=True,
            service_name="API", service_description="Test API.",
            service_outputs=["http://127.0.0.1:9120"],
            service_health={
                "type": "http", "url": "http://127.0.0.1:1/health",
                "startup_timeout_seconds": 0.1,
            },
        ))
    finally:
        terminal_tool._active_environments.pop("default", None)
        terminal_tool._last_activity.pop("default", None)

    assert result["status"] == "error"
    assert "readiness probe failed" in result["error"]
    assert registry.spawned["service_declaration"]["name"] == "API"
    assert registry.spawned["service_health"]["type"] == "http"
    assert registry.killed == ["proc_health"]
    assert registry.committed == []


def test_terminal_commits_healthy_service_as_one_invocation(monkeypatch, tmp_path):
    import tools.terminal_tool as terminal_tool
    from tools import process_registry as process_registry_module

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    registry = process_registry_module.ProcessRegistry()
    config = {
        "env_type": "local", "docker_image": "", "singularity_image": "",
        "modal_image": "", "daytona_image": "", "cwd": str(tmp_path), "timeout": 30,
    }
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool, "_check_all_guards", lambda *_args, **_kwargs: {"approved": True}
    )
    monkeypatch.setattr(process_registry_module, "process_registry", registry)
    monkeypatch.setattr(process_registry_module, "CHECKPOINT_PATH", tmp_path / "procs.json")
    monkeypatch.setitem(terminal_tool._active_environments, "default", SimpleNamespace(env={}))
    monkeypatch.setitem(terminal_tool._last_activity, "default", 0.0)
    try:
        result = json.loads(terminal_tool.terminal_tool(
            command=f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
            background=True,
            service_name="Healthy API", service_description="Integration test API.",
            service_outputs=[f"http://127.0.0.1:{port}"],
            service_health={
                "type": "http", "url": f"http://127.0.0.1:{port}/",
                "startup_timeout_seconds": 5,
            },
        ))
        services = registry.collect_service_declarations(probe_health=False)
    finally:
        if "result" in locals() and result.get("session_id"):
            registry.kill_process(result["session_id"])
        terminal_tool._active_environments.pop("default", None)
        terminal_tool._last_activity.pop("default", None)

    assert result["exit_code"] == 0
    assert result["health"]["status"] == "healthy"
    assert services[0]["label"] == "Healthy API"
    assert services[0]["health"]["status"] == "healthy"
