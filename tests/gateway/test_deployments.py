"""Deployments: a service's environments off this machine (``tui_gateway/deployments.py``).

Three things are load-bearing:

1. **The declaration is the only thing probed.** Health and revision come from the
   manifest's own probe (an HTTPS GET or a shell-less command); nothing else is
   contacted or run, and an unrunnable probe is ``unknown`` with the reason.
2. **A deployment is a graph node of its own**, one per environment, with the
   health its probe reports and a ``deploys`` relationship back to the codebase.
3. **Command log sinks are read once, bounded, never followed** — and only on a
   deployment; a machine-level manifest cannot declare one.
"""
import json
import logging
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.gateway.architecture_fixtures import local_manifest, minimal_document, write_manifest  # noqa: E402
from tui_gateway import architecture_store as store  # noqa: E402
from tui_gateway import deployments as dep  # noqa: E402
from tui_gateway import methods_service as ms  # noqa: E402
from tui_gateway import service_logs as logs  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    dep.reset_cache()
    yield root
    dep.reset_cache()


@pytest.fixture
def health_server():
    """A local HTTP server answering /healthz 200, /down 503, /version with JSON."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                body = b"ok"
                self.send_response(200)
            elif self.path == "/version":
                body = json.dumps({"build": {"sha": "9f2c1ab0000", "n": 1}}).encode()
                self.send_response(200)
            else:
                body = b"unavailable"
                self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _deployment(**extra):
    base = {
        "id": "prod",
        "environment": "production",
        "provider": "aws-lambda",
        "target": "arn:aws:lambda:us-east-1:1:function:demo",
        "health": {"kind": "command", "argv": ["true"]},
    }
    base.update(extra)
    return base


def _service(tmp_path, service_id="demo", **manifest_extra):
    root = tmp_path / service_id
    (root / "architecture" / "model").mkdir(parents=True, exist_ok=True)
    (root / "architecture" / "model" / "model.json").write_text(json.dumps(minimal_document()), encoding="utf-8")
    manifest = local_manifest(root, name=service_id.title())
    manifest.update(manifest_extra)
    write_manifest(store.manifests_dir(), service_id, manifest)
    return root, manifest


def _handlers(module=ms):
    pending = dict(module._registry._pending)
    for fn in pending.values():
        fn.__globals__["_ok"] = lambda rid, result: {"rid": rid, "result": result}
        fn.__globals__["_err"] = lambda rid, code, msg: {"rid": rid, "error": {"code": code, "message": msg}}
        fn.__globals__["_broadcast_global_event"] = lambda event, payload=None: None
        fn.__globals__.setdefault("logger", logging.getLogger("test"))
    return pending


# ── Declaration ──────────────────────────────────────────────────────────────


def test_a_deployment_normalises_with_defaults_and_a_graph_id():
    (deployment,) = dep.normalize_deployments([_deployment(url="https://demo.example.com", logs=[
        {"id": "cw", "kind": "command", "argv": "aws logs tail /aws/lambda/demo --since 15m"},
    ])], "demo")
    assert deployment["graph_id"] == "deploy:demo/prod"
    assert deployment["environment"] == "production"
    assert deployment["health"] == {"kind": "command", "argv": ["true"], "timeout_s": dep.DEFAULT_TIMEOUT_S}
    assert deployment["revision"] is None
    assert deployment["logs"][0]["kind"] == "command"
    assert deployment["logs"][0]["argv"] == ["aws", "logs", "tail", "/aws/lambda/demo", "--since", "15m"]
    assert deployment["logs"][0]["path"] is None


def test_https_probes_normalise_expect_and_refuse_plain_http():
    (deployment,) = dep.normalize_deployments([_deployment(
        health={"kind": "https", "url": "https://demo.example.com/healthz", "expect": 204, "timeout_s": 2},
        revision={"kind": "https", "url": "https://demo.example.com/version", "json_path": "build.sha"},
    )], "demo")
    assert deployment["health"] == {"kind": "https", "url": "https://demo.example.com/healthz", "expect": [204], "timeout_s": 2.0}
    assert deployment["revision"]["json_path"] == "build.sha"
    with pytest.raises(ValueError, match="https://"):
        dep.normalize_deployments([_deployment(health={"kind": "https", "url": "http://demo.example.com/healthz"})], "demo")


@pytest.mark.parametrize("bad, reason", [
    ({"id": "Prod"}, "id must match"),
    ({"provider": "AWS Lambda"}, "provider"),
    ({"target": ""}, "target is required"),
    ({"health": None}, "health is required"),
    ({"health": {"kind": "ping"}}, "health.kind"),
    ({"health": {"kind": "command", "argv": []}}, "argv"),
    ({"health": {"kind": "https", "url": "https://x", "expect": 42}}, "expect"),
    ({"health": {"kind": "command", "argv": ["true"], "timeout_s": -1}}, "timeout_s"),
    ({"revision": {"kind": "git"}}, "revision.kind"),
    ({"logs": [{"id": "f", "kind": "launchd_stdout"}]}, "launchd"),
    ({"logs": [{"id": "c", "kind": "command"}]}, "argv"),
])
def test_malformed_deployments_are_rejected(bad, reason):
    with pytest.raises(ValueError, match=reason):
        dep.normalize_deployments([_deployment(**bad)], "demo")


def test_duplicate_ids_and_non_lists_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        dep.normalize_deployments([_deployment(), _deployment()], "demo")
    with pytest.raises(ValueError, match="list"):
        dep.normalize_deployments({"id": "prod"}, "demo")
    assert dep.normalize_deployments(None, "demo") == []


def test_a_machine_level_manifest_cannot_declare_a_command_sink(tmp_path):
    with pytest.raises(ValueError, match="only valid on a deployment"):
        logs.normalize_log_sinks([{"id": "c", "kind": "command", "argv": ["true"]}], None)


def test_the_manifest_carries_deployments(home, tmp_path):
    _service(tmp_path, deployments=[_deployment(url="https://demo.example.com")])
    manifest = store.load_manifest("demo")
    assert [d["graph_id"] for d in manifest["deployments"]] == ["deploy:demo/prod"]
    assert dep.parse_graph_id("deploy:demo/prod") == ("demo", "prod")
    assert dep.parse_graph_id("deploy:demo") is None and dep.parse_graph_id("arch:demo") is None


# ── Probing ──────────────────────────────────────────────────────────────────


def test_https_health_is_live_when_the_status_is_expected(health_server):
    live = dep.probe_health({"kind": "https", "url": f"{health_server}/healthz", "expect": [200], "timeout_s": 2})
    assert live["status"] == "live" and "→ 200" in live["message"] and live["latency_ms"] >= 0
    down = dep.probe_health({"kind": "https", "url": f"{health_server}/down", "expect": [200], "timeout_s": 2})
    assert down["status"] == "down" and "expected 200" in down["message"]
    unreachable = dep.probe_health({"kind": "https", "url": "http://127.0.0.1:9/healthz", "expect": [200], "timeout_s": 1})
    assert unreachable["status"] == "down"


def test_command_health_follows_the_exit_code_and_a_missing_command_is_unknown():
    assert dep.probe_health({"kind": "command", "argv": ["true"], "timeout_s": 5})["status"] == "live"
    down = dep.probe_health({"kind": "command", "argv": ["false"], "timeout_s": 5})
    assert down["status"] == "down" and down["message"].startswith("exit 1")
    missing = dep.probe_health({"kind": "command", "argv": ["no-such-binary-xyz"], "timeout_s": 5})
    assert missing["status"] == "unknown" and "not found" in missing["message"]


def test_a_probe_that_hangs_is_down_not_an_exception():
    def hang(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    result = dep.probe_health({"kind": "command", "argv": ["sleep", "999"], "timeout_s": 0.5}, runner=hang)
    assert result["status"] == "down" and "timed out" in result["message"]


def test_revision_probes_read_stdout_or_a_json_path(health_server):
    assert dep.probe_revision({"kind": "command", "argv": ["printf", "9f2c1ab\\nnoise\\n"], "timeout_s": 5})["revision"] == "9f2c1ab"
    via_json = dep.probe_revision({"kind": "https", "url": f"{health_server}/version", "json_path": "build.sha", "timeout_s": 2})
    assert via_json["revision"] == "9f2c1ab0000"
    failed = dep.probe_revision({"kind": "command", "argv": ["false"], "timeout_s": 5})
    assert failed["revision"] is None and failed["message"].startswith("exit 1")


def test_probe_results_are_cached_until_forced(home, tmp_path, monkeypatch):
    _service(tmp_path, deployments=[_deployment(revision={"kind": "command", "argv": ["echo", "r1"]})])
    manifest = store.load_manifest("demo")
    deployment = manifest["deployments"][0]
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="r1\n", stderr="")

    first = dep.probe_deployment(manifest, deployment, runner=runner)
    again = dep.probe_deployment(manifest, deployment, runner=runner)
    assert first["health"]["status"] == "live" and first["revision"]["revision"] == "r1"
    assert again == first and len(calls) == 2  # health + revision, once
    dep.probe_deployment(manifest, deployment, force=True, runner=runner)
    assert len(calls) == 4
    assert dep.last_probe("deploy:demo/prod")["health"]["status"] == "live"


# ── Graph ────────────────────────────────────────────────────────────────────


def test_each_deployment_is_a_service_node_related_to_its_codebase(home, tmp_path):
    _service(tmp_path, deployments=[
        _deployment(url="https://demo.example.com"),
        _deployment(id="staging", environment="staging", target="arn:staging", health={"kind": "command", "argv": ["false"]}),
    ])
    services = dep.collect_deployment_services()
    assert [s["id"] for s in services] == ["deploy:demo/prod", "deploy:demo/staging"]
    prod, staging = services
    assert prod["label"] == "Demo · production" and prod["health"]["status"] == "live"
    assert staging["health"]["status"] == "down"
    assert prod["outputs"] == ["https://demo.example.com"] and staging["outputs"] == []
    assert prod["relationships"] == [{"predicate": "deploys", "object": "arch:demo"}]
    assert prod["architecture"]["ref"] == "arch:demo" and prod["architecture"]["deployment"] == "deploy:demo/prod"
    assert "**live**" in prod["description"] and "aws-lambda" in prod["description"]
    # The dataflow graph accepts them as service nodes.
    from cron.jobs import build_cron_graph
    graph = build_cron_graph(jobs=[], services=services)
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["deploy:demo/prod"]["kind"] == "service"
    assert nodes["deploy:demo/prod"]["health"]["status"] == "live"


def test_the_graph_overlay_includes_deployments(home, tmp_path, monkeypatch):
    _service(tmp_path, deployments=[_deployment()])
    import tools.service_graph as graph_mod
    monkeypatch.setattr(graph_mod, "collect_runtime_services", lambda: [])
    ids = [s["id"] for s in graph_mod.collect_graph_services()]
    assert "arch:demo" in ids and "deploy:demo/prod" in ids


def test_without_probing_an_unprobed_deployment_is_unknown(home, tmp_path):
    _service(tmp_path, deployments=[_deployment()])
    (service,) = dep.collect_deployment_services(probe=False)
    assert service["health"]["status"] == "unknown" and service["health"]["message"] == "not probed yet"


# ── Logs ─────────────────────────────────────────────────────────────────────


def test_a_deployment_resolves_its_command_sinks_and_reads_them_once(home, tmp_path):
    _service(tmp_path, deployments=[_deployment(logs=[
        {"id": "cw", "kind": "command", "argv": ["printf", "one\\ntwo\\nthree\\n"], "label": "CloudWatch"},
    ])])
    sinks = logs.service_sinks("deploy:demo/prod")
    assert [s["id"] for s in sinks] == ["cw"]
    resolved = logs.resolve_sink(sinks[0])
    assert resolved["kind"] == "command" and resolved["exists"] is True and resolved["argv"][0] == "printf"
    result = logs.read_logs(sinks, "deploy:demo/prod", None, 2, None)
    assert result["lines"] == ["two", "three"] and result["cursor"] is None and result["truncated"] is True
    assert result["exit_code"] == 0 and result["sink"]["label"] == "CloudWatch"
    with pytest.raises(logs.LogError) as exc:
        logs.start_follow(sinks, "deploy:demo/prod", None, lambda *a: None)
    assert exc.value.code == 4042
    with pytest.raises(logs.LogError) as unknown:
        logs.service_sinks("deploy:demo/nope")
    assert unknown.value.code == 4050
    with pytest.raises(logs.LogError) as missing:
        logs.service_sinks("deploy:ghost/prod")
    assert missing.value.code == 4030


def test_a_failing_or_missing_log_command_is_an_honest_4041():
    with pytest.raises(logs.LogError, match="not found") as missing:
        logs.read_command({"id": "cw", "kind": "command", "argv": ["no-such-binary-xyz"]}, 10)
    assert missing.value.code == 4041
    with pytest.raises(logs.LogError, match="exited 1"):
        logs.read_command({"id": "cw", "kind": "command", "argv": ["false"]}, 10)


def test_a_deployment_command_sink_satisfies_log_capture(home, tmp_path):
    _service(tmp_path, logs=[], deployments=[_deployment(logs=[{"id": "cw", "kind": "command", "argv": ["true"]}])])
    manifest = store.load_manifest("demo")
    # Capture enforcement is about the local runtime; the deployment's sinks belong
    # to the deployment and do not stand in for the machine's own logs.
    assert logs.capture_problem(manifest) == logs.NO_CAPTURE_PROBLEM
    assert logs.capture_problem({"root": "/x", "logs": manifest["deployments"][0]["logs"]}) is None


# ── Methods ──────────────────────────────────────────────────────────────────


def test_describe_and_the_service_methods_carry_deployments(home, tmp_path, health_server):
    _service(tmp_path, deployments=[_deployment(
        url="https://demo.example.com",
        health={"kind": "https", "url": f"{health_server}/healthz", "expect": [200], "timeout_s": 2},
        revision={"kind": "https", "url": f"{health_server}/version", "json_path": "build.sha", "timeout_s": 2},
    )])
    handlers = _handlers()
    listed = handlers["service.deployments"](1, {"service": "demo"})["result"]
    (before,) = listed["deployments"]
    assert before["graph_id"] == "deploy:demo/prod" and before["health"] is None and before["revision"] is None
    probed = handlers["service.deployments.probe"](2, {"service": "arch:demo"})["result"]
    (after,) = probed["deployments"]
    assert after["health"]["status"] == "live" and after["revision"] == "9f2c1ab0000" and after["probed_at"]
    described = store.describe(store.load_manifest("demo"))
    assert described["service"]["deployments"][0]["health"]["status"] == "live"
    # A stored revision the live probe answers with is marked live_in that environment.
    history = store.history(store.load_manifest("demo"))
    assert all("live_in" not in entry or entry["live_in"] == ["prod"] for entry in history["revisions"])
    missing = handlers["service.deployments.probe"](3, {"service": "demo", "deployment": "qa"})
    assert missing["error"]["code"] == 4050
    unknown = handlers["service.deployments"](4, {"service": "ghost"})
    assert unknown["error"]["code"] == 4030
