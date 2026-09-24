"""Architecture manifests bound to runtime services annotate the runtime node;
standalone manifests keep their own node (tools/service_graph.py)."""
import json
import logging

import pytest

from cron.changesets import configuration_digest
from cron.jobs import build_cron_graph
from tools import service_graph
from tools.architecture_services import collect_architecture_definitions, collect_architecture_services
from tui_gateway import architecture_store as store
from tui_gateway import files_browse


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _write_manifest(tmp_path, stem, runtime=None, **extra):
    root = tmp_path / stem
    (root / "architecture" / "model").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "build.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "architecture" / "model" / "model.json").write_text(json.dumps({
        "schema_version": "1.0.0", "components": [{"id": "a"}], "interplay": {"nodes": [], "edges": [], "invariants": []},
    }), encoding="utf-8")
    manifest = {"name": stem.title(), "description": f"The {stem} codebase.", "root": str(root),
                "source_files": ["scripts/build.py"], "outputs": ["https:127.0.0.1:7000"]}
    if runtime is not None:
        manifest["runtime"] = runtime
    manifest.update(extra)
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / f"{stem}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _runtime(service_id, **extra):
    service = {
        "id": service_id, "label": "Demo runtime", "description": "Runs the demo.",
        "inputs": ["file:~/.hermes/config.yaml"], "outputs": ["https:127.0.0.1:9000"], "side_effects": ["notify:ops"],
        "source_files": ["/opt/demo/serve.py"],
        "health": {"status": "healthy", "probe": "http", "target": "http://127.0.0.1:9000/health", "checked_at": "t", "latency_ms": 3, "message": ""},
    }
    service.update(extra)
    return service


# ── Binding validation ───────────────────────────────────────────────────────


def test_runtime_binding_resolves_each_provider_to_its_canonical_graph_id():
    assert store.normalize_runtime_binding({"provider": "launchd", "id": "ai.hermes.gateway"})["graph_id"] == "launchd:ai.hermes.gateway"
    assert store.normalize_runtime_binding({"provider": "docker", "id": "0123456789abcdef0123"})["graph_id"] == "docker:0123456789ab"
    assert store.normalize_runtime_binding({"provider": "nomad", "id": "dashboard"})["graph_id"] == "nomad:dashboard"
    assert store.normalize_runtime_binding({"provider": "process", "id": "proc_0123456789ab"})["graph_id"] == "proc_0123456789ab"
    assert store.normalize_runtime_binding({"provider": "process", "id": "0123456789ab"})["graph_id"] == "proc_0123456789ab"
    for bad, reason in (
        ({"provider": "kubernetes", "id": "x"}, "provider must be one of"),
        ({"provider": "launchd", "id": ""}, "non-empty"),
        ({"provider": "launchd", "id": "   "}, "non-empty"),
        ({"provider": "launchd", "id": 7}, "non-empty"),
        ({"provider": "launchd", "id": "launchd:already"}, "without the"),
        ({"provider": "launchd", "id": "arch:portal"}, "not a valid"),
        ({"provider": "launchd"}, "non-empty"),
        ("launchd:x", "must be an object"),
    ):
        with pytest.raises(ValueError, match=reason):
            store.normalize_runtime_binding(bad)


def test_manifest_carries_binding_and_annotation_names_the_runtime(home, tmp_path):
    _write_manifest(tmp_path, "demo", runtime={"provider": "launchd", "id": "demo"})
    manifest = store.load_manifest("demo")
    assert manifest["runtime"] == {"provider": "launchd", "id": "demo", "graph_id": "launchd:demo"}
    assert store.status_for(manifest)["runtime"] == "launchd:demo"
    assert store.status_for(manifest)["ref"] == "arch:demo", "the RPC reference stays the manifest id"
    store.manifests_dir().joinpath("broken.json").write_text(json.dumps({
        "name": "B", "description": "d", "root": str(tmp_path / "demo"), "runtime": {"provider": "k8s", "id": "x"},
    }), encoding="utf-8")
    assert [m["id"] for m in store.list_manifests()] == ["demo"], "a malformed binding skips the manifest with a warning"


# ── Composition ──────────────────────────────────────────────────────────────


def test_standalone_manifest_keeps_its_own_node(home, tmp_path):
    _write_manifest(tmp_path, "portal")
    definitions = collect_architecture_definitions()
    assert [d["graph_id"] for d in definitions] == ["arch:portal"] and definitions[0]["runtime"] is None
    services = service_graph.attach_architecture([_runtime("launchd:other")], definitions)
    assert [s["id"] for s in services] == ["launchd:other", "arch:portal"]
    assert "architecture" not in services[0]
    assert services[1]["architecture"]["ref"] == "arch:portal"
    assert collect_architecture_services()[0]["id"] == "arch:portal", "the standalone collector is unchanged"


@pytest.mark.parametrize("provider, runtime_id, graph_id", [
    ("launchd", "demo", "launchd:demo"),
    ("docker", "0123456789abcdef", "docker:0123456789ab"),
    ("nomad", "demo-job", "nomad:demo-job"),
    ("process", "proc_0123456789ab", "proc_0123456789ab"),
])
def test_bound_manifest_annotates_the_runtime_node_and_adds_none(home, tmp_path, provider, runtime_id, graph_id):
    root = _write_manifest(tmp_path, "demo", runtime={"provider": provider, "id": runtime_id})
    runtime = _runtime(graph_id, source_files=["/opt/demo/serve.py", str(root / "scripts/build.py")])
    services = service_graph.attach_architecture([runtime], collect_architecture_definitions())
    assert [s["id"] for s in services] == [graph_id], "one node per logical service"
    merged = services[0]
    # Runtime stays authoritative for what it owns.
    assert merged["health"]["status"] == "healthy"
    assert merged["description"] == "Runs the demo."
    assert merged["label"] == "Demo runtime"
    assert merged["inputs"] == runtime["inputs"] and merged["outputs"] == runtime["outputs"]
    assert merged["side_effects"] == runtime["side_effects"]
    # The manifest contributes the annotation and its code; the union is sorted and deduplicated.
    assert merged["architecture"]["ref"] == "arch:demo" and merged["architecture"]["runtime"] == graph_id
    assert merged["source_files"] == sorted({"/opt/demo/serve.py", str(root / "architecture/model/model.json"), str(root / "scripts/build.py")})
    assert runtime["source_files"] == ["/opt/demo/serve.py", str(root / "scripts/build.py")], "the provider's declaration is not mutated"
    assert collect_architecture_services() == [], "a bound definition is not a service of its own"


def test_bound_manifest_on_the_graph(home, tmp_path):
    root = _write_manifest(tmp_path, "demo", runtime={"provider": "launchd", "id": "demo"})
    runtime = _runtime("launchd:demo")
    services = service_graph.attach_architecture([runtime], collect_architecture_definitions())
    graph = build_cron_graph(jobs=[], services=services)
    ids = [n["id"] for n in graph["nodes"] if n["kind"] == "service"]
    assert ids == ["launchd:demo"] and "arch:demo" not in {n["id"] for n in graph["nodes"]}
    node = next(n for n in graph["nodes"] if n["id"] == "launchd:demo")
    assert node["health"]["status"] == "healthy"
    assert node["architecture"]["ref"] == "arch:demo"
    by_rel = {f.get("rel"): f for f in node["source_files"]}
    assert by_rel["scripts/build.py"]["root"] == "arch-demo" and by_rel["scripts/build.py"]["exists"] is True
    assert any(f["path"] == "/opt/demo/serve.py" and f["exists"] is False for f in node["source_files"])
    # Topology comes from the runtime declaration alone: the manifest's own outputs are not drawn.
    edges = {(e["source"], e["target"], e["type"]) for e in graph["edges"] if "launchd:demo" in (e["source"], e["target"])}
    assert ("launchd:demo", "https:127.0.0.1:9000", "writes") in edges
    assert not any("7000" in t for _, t, _ in edges), "the manifest's https:127.0.0.1:7000 output is not on the graph"
    assert files_browse.file_roots()["arch-demo"] == root.resolve(), "the model root is browsable regardless of binding"
    # The annotation stays outside the commitment digest.
    bare = build_cron_graph(jobs=[], services=[_runtime("launchd:demo")])
    assert configuration_digest(graph) == configuration_digest(bare)


def test_missing_runtime_target_hides_the_definition_without_fabricating_a_node(home, tmp_path, caplog):
    _write_manifest(tmp_path, "demo", runtime={"provider": "launchd", "id": "gone"})
    with caplog.at_level(logging.WARNING, logger="tools.service_graph"):
        services = service_graph.attach_architecture([_runtime("launchd:other")], collect_architecture_definitions())
    assert [s["id"] for s in services] == ["launchd:other"]
    assert "binds runtime service launchd:gone" in caplog.text
    # Same outcome through the composed collector with providers that report nothing.
    assert service_graph.collect_runtime_services(collectors={"launchd": lambda: []}) == []
    # The model itself stays reachable over the RPC by its manifest id.
    manifest = store.load_manifest("arch:demo")
    assert store.describe(manifest, git_runner=lambda _r: "r1")["service"]["runtime"]["graph_id"] == "launchd:gone"


def test_two_manifests_binding_one_runtime_are_both_rejected(home, tmp_path, caplog):
    _write_manifest(tmp_path, "alpha", runtime={"provider": "launchd", "id": "demo"})
    _write_manifest(tmp_path, "beta", runtime={"provider": "launchd", "id": "demo"})
    with caplog.at_level(logging.WARNING, logger="tools.service_graph"):
        services = service_graph.attach_architecture([_runtime("launchd:demo")], collect_architecture_definitions())
    assert [s["id"] for s in services] == ["launchd:demo"]
    assert "architecture" not in services[0], "no last-writer-wins"
    assert "2 manifests bind runtime service launchd:demo (alpha, beta)" in caplog.text


def test_source_file_union_is_deterministic():
    merged = service_graph._merged_source_files(["/b/two.py", "/a/one.py", "/a/./one.py", " "], ["/a/one.py", "/c/three.py"])
    assert merged == ["/a/one.py", "/b/two.py", "/c/three.py"]


def test_collect_runtime_services_is_ordered_and_fail_open(caplog):
    def boom():
        raise RuntimeError("docker down")

    collectors = {
        "nomad": lambda: [{"id": "nomad:z"}],
        "docker": boom,
        "launchd": lambda: [{"id": "launchd:a"}],
    }
    with caplog.at_level(logging.ERROR, logger="tools.service_graph"):
        services = service_graph.collect_runtime_services(collectors=collectors)
    assert [s["id"] for s in services] == ["launchd:a", "nomad:z"], "provider order, not completion order"
    assert "service overlay unavailable: docker" in caplog.text


def test_collect_graph_services_composes_end_to_end(home, tmp_path, monkeypatch):
    root = _write_manifest(tmp_path, "demo", runtime={"provider": "launchd", "id": "demo"})
    _write_manifest(tmp_path, "portal")
    monkeypatch.setattr(service_graph, "_runtime_collectors", lambda: {"launchd": lambda: [_runtime("launchd:demo")]})
    services = service_graph.collect_graph_services()
    assert [s["id"] for s in services] == ["launchd:demo", "arch:portal"]
    assert services[0]["architecture"]["ref"] == "arch:demo"
    assert str(root / "scripts/build.py") in services[0]["source_files"]


def test_rpc_identity_stays_the_manifest_id_for_bound_models(home, tmp_path):
    import tui_gateway.methods_architecture as ma

    _write_manifest(tmp_path, "demo", runtime={"provider": "launchd", "id": "demo"})
    pending = dict(ma._registry._pending)
    for fn in pending.values():
        fn.__globals__["_ok"] = lambda rid, result: {"result": result}
        fn.__globals__["_err"] = lambda rid, code, msg: {"error": {"code": code, "message": msg}}
        fn.__globals__["_broadcast_global_event"] = lambda event, payload=None: None
        fn.__globals__.setdefault("logger", logging.getLogger("t"))
    listed = pending["architecture.list"](1, {})["result"]["services"]
    assert listed[0]["id"] == "arch:demo" and listed[0]["runtime"]["graph_id"] == "launchd:demo"
    described = pending["architecture.describe"](2, {"service": "arch:demo"})["result"]
    assert described["service"]["id"] == "arch:demo" and described["service"]["runtime"]["graph_id"] == "launchd:demo"
    assert pending["architecture.history"](3, {"service": "demo"})["result"]["latest"] == described["revision"]
    assert pending["architecture.describe"](4, {"service": "launchd:demo"})["error"]["code"] == 4030, "the graph id is not the RPC id"
