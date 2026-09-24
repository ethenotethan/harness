"""Manifest-declared services join the dataflow graph with their code and model."""
import json

import pytest

from cron.jobs import build_cron_graph
from tools.architecture_services import collect_architecture_services
from tui_gateway import architecture_store as store
from tui_gateway import files_browse


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _write_service(tmp_path, **extra):
    root = tmp_path / "svc"
    (root / "architecture" / "model").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "build.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "architecture" / "model" / "model.json").write_text(json.dumps({
        "schema_version": "1.0.0", "components": [], "interplay": {"nodes": [], "edges": [], "invariants": []},
    }), encoding="utf-8")
    manifest = {
        "name": "Demo", "description": "A **demo** service.", "root": str(root),
        "source_files": ["scripts/build.py", "scripts/missing.py"],
        "inputs": ["file:~/.hermes/config.yaml"], "outputs": ["https:127.0.0.1:9000"], "side_effects": ["notify:ops"],
    }
    manifest.update(extra)
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / "demo.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_collector_declares_the_service_with_absolute_source_files(home, tmp_path):
    root = _write_service(tmp_path)
    (service,) = collect_architecture_services()
    assert service["id"] == "arch:demo" and service["label"] == "Demo"
    assert service["description"] == "A **demo** service."
    assert service["inputs"] == ["file:~/.hermes/config.yaml"] and service["outputs"] == ["https:127.0.0.1:9000"]
    assert service["side_effects"] == ["notify:ops"]
    assert service["source_files"] == sorted({
        str(root / "architecture/model/model.json"), str(root / "scripts/build.py"), str(root / "scripts/missing.py"),
    })
    assert service["architecture"]["ref"] == "arch:demo" and service["architecture"]["source"] == "local"


def test_github_service_has_no_local_files(home, tmp_path):
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / "remote.json").write_text(json.dumps({
        "name": "Remote", "description": "Lives on GitHub.", "repository": "o/r", "ref": "main",
    }), encoding="utf-8")
    (service,) = collect_architecture_services()
    assert service["source_files"] == [] and service["architecture"]["source"] == "github"
    assert service["architecture"]["revision"] == "main"


def test_collector_skips_a_manifest_the_graph_rejects(home, tmp_path):
    _write_service(tmp_path, outputs=["cron-output:reserved"])
    assert collect_architecture_services() == []


def test_service_node_carries_files_and_architecture_and_its_root_is_browsable(home, tmp_path):
    root = _write_service(tmp_path)
    roots = files_browse.file_roots()
    assert roots["arch-demo"] == root.resolve(), "the checkout is a read-only browse root"
    graph = build_cron_graph(jobs=[], services=collect_architecture_services())
    node = next(n for n in graph["nodes"] if n["id"] == "arch:demo")
    assert node["kind"] == "service" and node["architecture"]["ref"] == "arch:demo"
    by_rel = {f["rel"]: f for f in node["source_files"]}
    assert by_rel["scripts/build.py"]["root"] == "arch-demo" and by_rel["scripts/build.py"]["exists"] is True
    assert by_rel["scripts/missing.py"]["exists"] is False, "a declared file that does not exist yet is listed, not dropped"
    assert by_rel["architecture/model/model.json"]["role"] == "declared"
    listing = files_browse.list_tree("arch-demo", "scripts")
    assert [e["path"] for e in listing["entries"]] == ["scripts/build.py"]
    assert files_browse.read_file("arch-demo", "scripts/build.py")["content"] == "print('hi')\n"


def test_missing_root_directory_is_not_advertised(home, tmp_path):
    store.manifests_dir().mkdir(parents=True, exist_ok=True)
    (store.manifests_dir() / "gone.json").write_text(json.dumps({
        "name": "Gone", "description": "Root was deleted.", "root": str(tmp_path / "nowhere"),
    }), encoding="utf-8")
    assert "arch-gone" not in files_browse.file_roots()
