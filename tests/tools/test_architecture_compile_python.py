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

STREAM = "rtsp://camera.local/stream"


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
        requests.post("https://api.telegram.org/bot/sendMessage", json={"text": response.text})
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
    "external_systems": [
        {"id": "rtsp-camera", "label": "RTSP camera", "category": "camera", "protocol": "RTSP",
         "description": "The PTZ camera the service pulls frames from.", "signatures": [{"pattern": "rtsp://", "scope": "strings"}]},
        {"id": "telegram", "label": "Telegram Bot API", "category": "messaging",
         "description": "Where room events are published.", "signatures": [{"pattern": "api\\.telegram\\.org", "scope": "strings"}]},
        {"id": "http-lib", "label": "requests", "category": "http",
         "description": "The HTTP client library every outbound call goes through.", "signatures": ["\\brequests\\b"]},
    ],
    "external_groups": [
        {"id": "cameras", "label": "Cameras", "description": "Video sources.", "categories": ["camera"]},
        {"id": "messaging", "label": "Messaging", "description": "Outbound publication.", "categories": ["messaging", "http"]},
    ],
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
    assert nodes["class:core:Store"]["path"] == "app/core.py" and nodes["class:core:Store"]["line"] == 16
    assert nodes["class:core:Store.Inner"]["label"] == "Store.Inner"
    assert nodes["class:core:State"]["sub_kind"] == "state_machine"
    assert nodes["entrypoint:web:main.py"]["kind"] == "entrypoint"
    assert nodes["external:flask"]["kind"] == "external" and nodes["external:flask"]["sub_kind"] == "package" and nodes["external:flask"]["component"] is None
    assert "external:requests" not in nodes, "a declared system whose signature matches the import absorbs the package"
    assert "external:sqlite3" not in nodes, "the standard library is not an external boundary"
    assert "external:app" not in nodes, "the service's own package is not external"
    store = next(n for n in nodes.values() if n["kind"] == "store")
    assert store["sub_kind"] == "sqlite" and store["owner_type"] == "Store"
    resource = next(n for n in nodes.values() if n["kind"] == "resource" and n["label"] == "requests.get")
    assert resource["label"] == "requests.get" and resource["sub_kind"] == "http_client"
    endpoints = sorted(n["label"] for n in nodes.values() if n["kind"] == "endpoint")
    assert endpoints == ["GET /frames", "ROUTE /health"]
    # Wiring: structure, lifecycle, boundary, usage.
    assert edges[("module:core:app/core.py", "class:core:Store", "declares")] == "structure"
    assert edges[("entrypoint:web:main.py", "class:core:Store", "constructs")] == "structure"
    assert edges[("class:core:Store", "external:http-lib", "uses")] == "boundary"
    assert edges[("module:web:app/web.py", "external:flask", "uses")] == "boundary"
    assert edges[("class:core:Store", store["id"], "persists-to")] == "lifecycle"
    assert edges[("class:core:Store", resource["id"], "holds")] == "lifecycle"
    assert edges[("class:core:Store", "class:core:State", "transitions")] == "lifecycle"
    assert edges[("module:web:main.py", "class:core:Store", "drives")] == "usage"
    # Provenance: one entity per node, origins cite the lines the passes fired on.
    entities = {e["id"]: e for e in model["extraction"]["entities"]}
    assert set(entities) == set(nodes)
    assert {(o["path"], o["line"], o["rule"]) for o in entities["class:core:Store"]["origins"]} == {("app/core.py", 16, "py.class")}
    state_origins = {(o["line"], o["rule"]) for o in entities["class:core:State"]["origins"]}
    assert (11, "py.class") in state_origins and (20, "py.state_machine") in state_origins and (24, "py.state_machine") in state_origins
    assert entities["declared:camera"]["origins"] == [{"path": "app/core.py", "line": 12, "rule": "declared.node", "family": "wiring"}]
    passes = {p["id"]: p for p in model["extraction"]["passes"]}
    for rule in ("py.module", "py.class", "py.entrypoint", "py.route", "py.subprocess", "py.file_write", "py.http_client",
                 "py.thread_or_task", "py.env_read", "py.import_external", "py.state_machine", "py.persistent_store", "py.boundary.external_signature"):
        assert rule in passes and passes[rule]["class"] == "mechanical", rule
    assert passes["declared.node"]["class"] == "semantic"
    core_file = next(f for f in model["extraction"]["files"] if f["path"] == "app/core.py")
    assert core_file["touched"] and core_file["semantic_citations"] == 1 and "declared.node" not in core_file["passes"]
    fetch = next(d for d in core_file["declarations"] if d["name"] == "Store.fetch")
    assert set(fetch["passes"]) == {"py.file_write", "py.http_client", "py.state_machine", "py.boundary.external_signature"}
    init_file = next(f for f in model["extraction"]["files"] if f["path"] == "app/__init__.py")
    assert init_file["touched"] and init_file["passes"] == ["py.module"], "an empty module is still a construction"
    summary = model["extraction"]["summary"]
    assert summary["entities"] == summary["entities_with_origin"] == len(nodes)
    assert summary["passes"] == len([p for p in passes.values() if p["class"] == "mechanical"])
    # Flows, invariants, components, inventory.
    assert [f["id"] for f in model["interplay"]["flows"]] == ["boot"] and model["interplay"]["flows"][0]["authority"] == "declared"
    statuses = {i["id"]: i["status"] for i in model["interplay"]["invariants"]}
    assert statuses["components-assign-every-file"] == "holds" and statuses["single-camera"] == "unchecked" and statuses["gates-declared"] == "holds"
    assert statuses["externals-declared-and-observed"] == "holds" and [i["id"] for i in model["interplay"]["invariants"]] == sorted(statuses)
    assert [c["id"] for c in model["components"]] == ["core", "web"]
    assert model["components"][0]["files"] == ["app/__init__.py", "app/core.py"] and model["components"][0]["declarations"] == ["State", "Store", "Store.Inner"]
    assert model["inventory"]["files"] == 4 and model["inventory"]["lines"] > 0
    assert model["contract"] == {"name": "hermes.architecture", "version": "1.0"}


def test_declared_external_systems_are_observed_grouped_and_wired(service):
    model = compiler.compile_service(service)
    assert validate_document(model) == []
    systems = {x["id"]: x for x in model["externals"]["systems"]}
    assert set(systems) == {"rtsp-camera", "telegram", "http-lib"}
    camera = systems["rtsp-camera"]
    assert camera["hit_count"] == 1 and camera["paths"] == ["app/core.py"] and camera["component"] == "core" and camera["protocol"] == "RTSP"
    assert camera["authority"] == "observed" and camera["description_authority"] == "specified"
    assert systems["telegram"]["hit_count"] == 1 and systems["telegram"]["file_count"] == 1
    http = systems["http-lib"]
    assert http["hit_count"] == 4, "import line (twice: signature + absorbed import), requests.get, requests.post"
    assert http["component"] == "core" and http["signatures"] == [{"pattern": "\\brequests\\b", "scope": "code"}]
    nodes = {n["id"]: n for n in model["interplay"]["nodes"]}
    for system_id, category in (("rtsp-camera", "camera"), ("telegram", "messaging"), ("http-lib", "http")):
        node = nodes[f"external:{system_id}"]
        assert node["kind"] == "external" and node["sub_kind"] == "system" and node["owner_type"] == "External systems" and node["category"] == category
    assert nodes["external:rtsp-camera"]["path"] == "app/core.py" and nodes["external:rtsp-camera"]["line"] == 8
    edges = {(e["source"], e["target"], e["relation"]): e["class"] for e in model["interplay"]["edges"]}
    assert edges[("module:core:app/core.py", "external:rtsp-camera", "uses")] == "boundary", "a module-level hit is the module's"
    assert edges[("class:core:Store", "external:telegram", "uses")] == "boundary", "a hit inside a method is the class's"
    assert edges[("class:core:Store", "external:http-lib", "uses")] == "boundary"
    assert edges[("module:core:app/core.py", "external:http-lib", "uses")] == "boundary", "the import line is module-level"
    assert ("class:core:Store", "external:requests", "uses") not in edges
    entities = {e["id"]: e for e in model["extraction"]["entities"]}
    assert entities["external:rtsp-camera"]["origins"] == [{"path": "app/core.py", "line": 8, "rule": "py.boundary.external_signature", "family": "boundary"}]
    http_origins = {(o["line"], o["rule"]) for o in entities["external:http-lib"]["origins"]}
    assert http_origins == {(6, "py.boundary.external_signature"), (6, "py.import_external"), (25, "py.boundary.external_signature"), (26, "py.boundary.external_signature")}
    assert entities["external:telegram"]["origins"][0]["line"] == 26
    # The extraction map sees the new pass; string-scoped hits never fire on code.
    passes = {p["id"]: p for p in model["extraction"]["passes"]}
    assert passes["py.boundary.external_signature"]["class"] == "mechanical" and passes["py.boundary.external_signature"]["citations"] == 4, "citations are unique (file, line) per pass: line 26 hits two systems once"
    core_file = next(f for f in model["extraction"]["files"] if f["path"] == "app/core.py")
    assert "py.boundary.external_signature" in core_file["passes"]
    # Boundary groups: members are the external nodes whose category the group lists.
    groups = {g["id"]: g for g in model["interplay"]["boundary_groups"]}
    assert groups["cameras"]["members"] == ["external:rtsp-camera"]
    assert groups["messaging"]["members"] == ["external:http-lib", "external:telegram"]
    assert groups["messaging"]["categories"] == ["http", "messaging"] and groups["cameras"]["description"] == "Video sources."
    assert model["externals"]["groups"] == [{k: groups[g][k] for k in ("id", "label", "description", "categories")} for g in ("cameras", "messaging")]
    assert nodes["external:flask"]["category"] == "package", "an unabsorbed package keeps its own node and category"


def test_signature_scopes_mask_comments_and_strings():
    masked, strings = compiler.mask_source('x = "rtsp://a"  # rtsp://comment\ny = f"{x}/live"\nz = """multi\nline"""\n')
    assert "rtsp://" not in masked and "comment" not in masked, "string contents and comments are blanked in code scope"
    assert masked.count("\n") == 4, "newlines survive so line numbers do"
    assert (1, "rtsp://a") in strings and (3, "multi\nline") in strings
    text, no_strings = compiler.mask_source("def broken(:\n")
    assert text == "def broken(:\n" and no_strings == [], "a file that does not tokenize is matched raw"


def test_external_declarations_fail_closed(tmp_path):
    unobserved = json.loads(json.dumps(CONFIG))
    unobserved["external_systems"].append({"id": "mqtt", "label": "MQTT broker", "category": "bus", "description": "Never used.",
                                           "signatures": [{"pattern": "mqtt://", "scope": "strings"}]})
    with pytest.raises(compiler.CompileError, match="external system 'mqtt' is declared but none of its signatures matches"):
        compiler.compile_service(write_service(tmp_path / "a", unobserved))
    empty_group = json.loads(json.dumps(CONFIG))
    empty_group["external_groups"].append({"id": "printers", "label": "Printers", "categories": ["printer"]})
    with pytest.raises(compiler.CompileError, match="external group 'printers' has no member"):
        compiler.compile_service(write_service(tmp_path / "b", empty_group))
    shared_category = json.loads(json.dumps(CONFIG))
    shared_category["external_groups"].append({"id": "video", "label": "Video", "categories": ["camera"]})
    with pytest.raises(compiler.CompileError, match="category 'camera' belongs to both"):
        compiler.compile_service(write_service(tmp_path / "c", shared_category))
    bad_regex = json.loads(json.dumps(CONFIG))
    bad_regex["external_systems"][0]["signatures"] = ["("]
    with pytest.raises(compiler.CompileError, match="not a valid regex"):
        compiler.compile_service(write_service(tmp_path / "d", bad_regex))


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
