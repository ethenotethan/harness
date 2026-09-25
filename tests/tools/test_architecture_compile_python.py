"""The Python-service architecture compiler emits a conforming hermes.architecture
document: components fail closed, the extraction map has provenance for every
construction, declared additions must resolve, gates are wired into one merge
gate, --check detects drift, and the output is byte-deterministic."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import architecture_compile_python as compiler
from tui_gateway.architecture_contract import validate_document

REPO = Path(__file__).resolve().parents[2]

CORE = '''"""Core of the demo service."""
import enum
import os
import sqlite3

import requests


class State(enum.Enum):
    IDLE = "idle"
    BUSY = "busy"


class Store:
    """Keeps frames."""

    def __init__(self):
        self.state = State.IDLE
        self.db = sqlite3.connect(os.environ.get("DEMO_DB", "demo.db"))

    def fetch(self):
        self.state = State.BUSY
        response = requests.get("https://example.test/frames")
        with open("last.json", "w") as handle:
            handle.write(response.text)
        return response

    class Inner:
        def helper(self):
            return 1
'''

WEB = '''from flask import Flask

app = Flask(__name__)


@app.route("/health")
def health():
    return "ok"


@app.get("/frames")
def frames():
    return "[]"
'''

MAIN = '''import subprocess
import threading

from app.core import Store


def main():
    store = Store()
    threading.Thread(target=store.fetch).start()
    subprocess.run(["true"])


if __name__ == "__main__":
    main()
'''

CONFIG = {
    "title": "Demo",
    "description": "A demo Python service.",
    "repository": None,
    "components": [
        {"id": "core", "label": "Core", "description": "Frames and state.", "patterns": ["app/core.py", "app/__init__.py"]},
        {"id": "web", "label": "Web", "description": "HTTP surface and entrypoint.", "patterns": ["app/web.py", "main.py"]},
    ],
    "layers": [{"id": "service", "label": "Service", "order": 1}],
    "declared": {
        "nodes": [{"id": "declared:camera", "kind": "device", "label": "The camera", "component": "core", "path": "app/core.py", "line": 12}],
        "edges": [{"source": "module:web:main.py", "target": "class:core:Store", "relation": "drives"},
                  {"source": "class:core:Store", "target": "declared:camera", "relation": "reads"}],
        "flows": [{"id": "boot", "title": "Boot", "steps": [
            {"from": "module:web:main.py", "to": "class:core:Store", "relation": "drives"},
            {"from": "class:core:Store", "to": "declared:camera", "relation": "reads", "note": "first frame"}]}],
        "invariants": [{"id": "single-camera", "kind": "single_camera", "why": "One camera per host."}],
    },
    "gates": {"commands": [
        {"id": "check", "name": "Model is current", "command": "python3 tools/architecture_compile_python.py . --check"},
        {"id": "tests", "name": "Unit tests", "command": "python3 -m pytest -q", "role": "gate"},
    ]},
}


def write_service(root: Path, config=CONFIG) -> Path:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "core.py").write_text(CORE, encoding="utf-8")
    (root / "app" / "web.py").write_text(WEB, encoding="utf-8")
    (root / "main.py").write_text(MAIN, encoding="utf-8")
    (root / "architecture").mkdir(exist_ok=True)
    (root / "architecture" / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return root


@pytest.fixture
def service(tmp_path):
    return write_service(tmp_path / "svc")


def test_model_conforms_and_maps_every_construction(service):
    model = compiler.compile_service(service)
    assert validate_document(model) == []
    nodes = {n["id"]: n for n in model["interplay"]["nodes"]}
    edges = {(e["source"], e["target"], e["relation"]): e["class"] for e in model["interplay"]["edges"]}
    # Constructions the passes found.
    assert nodes["class:core:Store"]["path"] == "app/core.py" and nodes["class:core:Store"]["line"] == 14
    assert nodes["class:core:Store.Inner"]["label"] == "Store.Inner"
    assert nodes["class:core:State"]["sub_kind"] == "state_machine"
    assert nodes["entrypoint:web:main.py"]["kind"] == "entrypoint"
    assert nodes["external:requests"]["kind"] == "external" and nodes["external:flask"]["component"] is None
    assert "external:sqlite3" not in nodes, "the standard library is not an external boundary"
    assert "external:app" not in nodes, "the service's own package is not external"
    store = next(n for n in nodes.values() if n["kind"] == "store")
    assert store["sub_kind"] == "sqlite" and store["owner_type"] == "Store"
    resource = next(n for n in nodes.values() if n["kind"] == "resource")
    assert resource["label"] == "requests.get" and resource["sub_kind"] == "http_client"
    endpoints = sorted(n["label"] for n in nodes.values() if n["kind"] == "endpoint")
    assert endpoints == ["GET /frames", "ROUTE /health"]
    # Wiring: structure, lifecycle, boundary, usage.
    assert edges[("module:core:app/core.py", "class:core:Store", "declares")] == "structure"
    assert edges[("entrypoint:web:main.py", "class:core:Store", "constructs")] == "structure"
    assert edges[("class:core:Store", "external:requests", "uses")] == "boundary"
    assert edges[("module:web:app/web.py", "external:flask", "uses")] == "boundary"
    assert edges[("class:core:Store", store["id"], "persists-to")] == "lifecycle"
    assert edges[("class:core:Store", resource["id"], "holds")] == "lifecycle"
    assert edges[("class:core:Store", "class:core:State", "transitions")] == "lifecycle"
    assert edges[("module:web:main.py", "class:core:Store", "drives")] == "usage"
    # Provenance: one entity per node, origins cite the lines the passes fired on.
    entities = {e["id"]: e for e in model["extraction"]["entities"]}
    assert set(entities) == set(nodes)
    assert {(o["path"], o["line"], o["rule"]) for o in entities["class:core:Store"]["origins"]} == {("app/core.py", 14, "py.class")}
    state_origins = {(o["line"], o["rule"]) for o in entities["class:core:State"]["origins"]}
    assert (9, "py.class") in state_origins and (18, "py.state_machine") in state_origins and (22, "py.state_machine") in state_origins
    assert entities["declared:camera"]["origins"] == [{"path": "app/core.py", "line": 12, "rule": "declared.node", "family": "wiring"}]
    passes = {p["id"]: p for p in model["extraction"]["passes"]}
    for rule in ("py.module", "py.class", "py.entrypoint", "py.route", "py.subprocess", "py.file_write", "py.http_client",
                 "py.thread_or_task", "py.env_read", "py.import_external", "py.state_machine", "py.persistent_store"):
        assert rule in passes and passes[rule]["class"] == "mechanical", rule
    assert passes["declared.node"]["class"] == "semantic"
    core_file = next(f for f in model["extraction"]["files"] if f["path"] == "app/core.py")
    assert core_file["touched"] and core_file["semantic_citations"] == 1 and "declared.node" not in core_file["passes"]
    fetch = next(d for d in core_file["declarations"] if d["name"] == "Store.fetch")
    assert set(fetch["passes"]) == {"py.file_write", "py.http_client", "py.state_machine"}
    init_file = next(f for f in model["extraction"]["files"] if f["path"] == "app/__init__.py")
    assert init_file["touched"] and init_file["passes"] == ["py.module"], "an empty module is still a construction"
    summary = model["extraction"]["summary"]
    assert summary["entities"] == summary["entities_with_origin"] == len(nodes)
    assert summary["passes"] == len([p for p in passes.values() if p["class"] == "mechanical"])
    # Flows, invariants, components, inventory.
    assert [f["id"] for f in model["interplay"]["flows"]] == ["boot"] and model["interplay"]["flows"][0]["authority"] == "declared"
    statuses = {i["id"]: i["status"] for i in model["interplay"]["invariants"]}
    assert statuses["components-assign-every-file"] == "holds" and statuses["single-camera"] == "unchecked" and statuses["gates-declared"] == "holds"
    assert [c["id"] for c in model["components"]] == ["core", "web"]
    assert model["components"][0]["files"] == ["app/__init__.py", "app/core.py"] and model["components"][0]["declarations"] == ["State", "Store", "Store.Inner"]
    assert model["inventory"]["files"] == 4 and model["inventory"]["lines"] > 0
    assert model["contract"] == {"name": "hermes.architecture", "version": "1.0"}


def test_gates_wire_into_one_merge_gate(service):
    ci = compiler.compile_service(service)["ci"]
    assert [w["id"] for w in ci["workflows"]] == ["local"]
    jobs = {j["id"]: j for j in ci["jobs"]}
    assert jobs["local/check"]["role"] == "local" and jobs["local/tests"]["role"] == "gate"
    assert ci["merge"] == {"id": "merge:snapshot", "label": "Snapshot accepted", "inputs": ["local/check", "local/tests"]}
    kinds = {(e["source"], e["target"]): e["kind"] for e in ci["edges"]}
    assert kinds[("trigger:snapshot", "local/check")] == "trigger" and kinds[("local/tests", "merge:snapshot")] == "gates"
    assert ci["static_checks"][0]["job"] == "local/check" and "--check" in ci["static_checks"][0]["command"]
    assert ci["summary"] == {"workflows": 1, "jobs": 2, "gates": 2, "ratchets": 0, "static_checks": 1}


def test_github_workflows_join_the_gates(tmp_path):
    root = write_service(tmp_path / "svc", {**CONFIG, "gates": {"commands": CONFIG["gates"]["commands"], "workflows": ".github/workflows",
                                                                   "workflow_families": {"tests": "behavior"}}})
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "tests.yml").write_text(
        "name: Tests\non:\n  pull_request:\n    branches: [main]\n  push:\n    branches: [main]\njobs:\n"
        "  unit:\n    name: Unit\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - name: Test\n        run: python3 -m pytest\n"
        "  lint:\n    needs: unit\n    runs-on: ubuntu-latest\n    steps:\n      - run: ruff check .\n", encoding="utf-8")
    (root / ".github" / "workflows" / "deploy.yml").write_text(
        "name: Deploy\non:\n  push:\n    branches: [main]\njobs:\n  ship:\n    runs-on: ubuntu-latest\n    if: false\n    steps:\n      - run: ./deploy.sh\n", encoding="utf-8")
    model = compiler.compile_service(root)
    assert validate_document(model) == []
    ci = model["ci"]
    jobs = {j["id"]: j for j in ci["jobs"]}
    assert jobs["tests/unit"]["role"] == "gate" and jobs["tests/lint"]["needs"] == ["tests/unit"] and jobs["tests/lint"]["family"] == "behavior"
    assert jobs["deploy/ship"]["role"] == "disabled" and jobs["deploy/ship"]["condition"] == "false"
    assert jobs["tests/unit"]["steps"][1] == {"name": "Test", "line": 0, "command": "python3 -m pytest"}
    assert jobs["tests/unit"]["evidence"] == {"path": ".github/workflows/tests.yml", "line": 8}
    assert ci["merge"]["inputs"] == ["local/check", "local/tests", "tests/lint", "tests/unit"]
    kinds = {(e["source"], e["target"]): e["kind"] for e in ci["edges"]}
    assert kinds[("tests/unit", "tests/lint")] == "needs" and kinds[("trigger:pull_request", "tests/unit")] == "trigger"
    assert ("trigger:pull_request", "tests/lint") not in kinds, "a job with needs is fed by its dependency, not the trigger"
    assert [t["id"] for t in ci["triggers"]] == ["trigger:pull_request", "trigger:push", "trigger:snapshot"]


def test_unassigned_file_fails_closed(tmp_path):
    root = write_service(tmp_path / "svc")
    (root / "stray.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(compiler.CompileError, match="unassigned: stray.py"):
        compiler.compile_service(root)


def test_declared_additions_must_resolve(tmp_path):
    bad_edge = json.loads(json.dumps(CONFIG))
    bad_edge["declared"]["edges"].append({"source": "class:core:Store", "target": "class:core:Ghost", "relation": "haunts"})
    with pytest.raises(compiler.CompileError, match="Ghost"):
        compiler.compile_service(write_service(tmp_path / "a", bad_edge))
    bad_flow = json.loads(json.dumps(CONFIG))
    bad_flow["declared"]["flows"][0]["steps"].append({"from": "class:core:Store", "to": "class:core:State", "relation": "ignores"})
    with pytest.raises(compiler.CompileError, match="no edge class:core:Store -ignores-> class:core:State"):
        compiler.compile_service(write_service(tmp_path / "b", bad_flow))
    bad_node = json.loads(json.dumps(CONFIG))
    bad_node["declared"]["nodes"][0]["path"] = "nowhere.py"
    with pytest.raises(compiler.CompileError, match="analysed file"):
        compiler.compile_service(write_service(tmp_path / "c", bad_node))
    no_gates = json.loads(json.dumps(CONFIG))
    no_gates["gates"] = {"commands": []}
    with pytest.raises(compiler.CompileError, match="at least one gate"):
        compiler.compile_service(write_service(tmp_path / "d", no_gates))


def test_cli_writes_checks_and_is_byte_deterministic(service):
    tool = str(REPO / "tools" / "architecture_compile_python.py")
    first = subprocess.run([sys.executable, tool, str(service)], capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    model_path = service / "architecture" / "model" / "model.json"
    written = model_path.read_bytes()
    assert validate_document(json.loads(written)) == []
    again = subprocess.run([sys.executable, tool, str(service)], capture_output=True, text=True, check=False)
    assert again.returncode == 0 and model_path.read_bytes() == written, "compiling twice yields identical bytes"
    check = subprocess.run([sys.executable, tool, str(service), "--check"], capture_output=True, text=True, check=False)
    assert check.returncode == 0 and "is current" in check.stdout
    (service / "app" / "core.py").write_text(CORE + "\n\nclass Late:\n    pass\n", encoding="utf-8")
    stale = subprocess.run([sys.executable, tool, str(service), "--check"], capture_output=True, text=True, check=False)
    assert stale.returncode == 1 and "stale" in stale.stderr
    (service / "stray.py").write_text("x = 1\n", encoding="utf-8")
    failed = subprocess.run([sys.executable, tool, str(service)], capture_output=True, text=True, check=False)
    assert failed.returncode == 1 and "unassigned" in failed.stderr
    assert model_path.read_bytes() == written, "a failed compile never overwrites the committed model"
