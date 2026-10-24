"""The multi-language architecture compiler: one core, one language pack per
language, every output conforming to the hermes.architecture contract."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter", reason="the architecture compiler needs tree-sitter (pip install -r tools/architecture_compiler/requirements.txt)")
pytest.importorskip("tree_sitter_language_pack", reason="the architecture compiler needs tree-sitter-language-pack")

from tools.architecture_compile_python import CompileError  # noqa: E402
from tools.architecture_compiler import compile_service, serialized  # noqa: E402
from tools.architecture_compiler.core import CHECK_COMMAND, load_extended_config, project_rule  # noqa: E402
from tools.architecture_compiler.packs import PACKS, pack_for_path  # noqa: E402
from tools.architecture_compiler.packs.base import FAMILIES, Rule  # noqa: E402
from tui_gateway.architecture_contract import validate_document  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

GATES = {"commands": [{"id": "check", "name": "Model current", "command": CHECK_COMMAND}]}


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def config(root: Path, **overrides):
    document = {
        "title": "Fixture", "description": "A fixture service.", "repository": None,
        "components": [{"id": "core", "label": "Core", "description": "Everything.", "patterns": ["**"]}],
        "gates": GATES,
    }
    document.update(overrides)
    write(root, "architecture/config.json", json.dumps(document, indent=2))


def nodes_by_kind(model, kind):
    return sorted(n["id"] for n in model["interplay"]["nodes"] if n["kind"] == kind)


def edges(model, relation=None):
    return {(e["source"], e["target"], e["relation"], e["class"]) for e in model["interplay"]["edges"] if relation is None or e["relation"] == relation}


def passes(model):
    return {p["id"]: p for p in model["extraction"]["passes"]}


def origins_of(model, node_id):
    entity = next(e for e in model["extraction"]["entities"] if e["id"] == node_id)
    return {(o["path"], o["line"], o["rule"], o["family"]) for o in entity["origins"]}


# ── Swift ────────────────────────────────────────────────────────────────────

SWIFT_STORE = '''import Foundation
import MLX

enum Phase { case idle, loading }

final class ActivityStore: ObservableObject {
    @Published var state: Phase = .idle
    private let key = "portal.items"
    let socket = "wss://gateway.local/v1/ws"
    func send() async throws {
        let response = try await client.call("chat.send", params: [:])
        state = .loading
        UserDefaults.standard.set(1, forKey: key)
        Task { await self.load() }
        let session = URLSession.shared
        _ = MLX.array([1])
    }
}
'''
SWIFT_VIEW = '''import SwiftUI

struct ChatView: View {
    @StateObject var model: ActivityStore
    var body: some View {
        Button("Send") { Task { try? await model.send() } }
            .onAppear { model.state = .idle }
    }
}

@main struct DemoApp: App { var body: some Scene { WindowGroup { ChatView(model: ActivityStore()) } } }
'''


def swift_service(tmp_path: Path) -> Path:
    root = tmp_path / "swift"
    config(root,
           components=[{"id": "app", "label": "App", "description": "UI.", "patterns": ["Sources/App/**"]},
                       {"id": "store", "label": "Store", "description": "State.", "patterns": ["Sources/Store/**"]}],
           external_systems=[
               {"id": "gateway", "label": "Gateway", "category": "backend", "description": "RPC backend.", "signatures": [{"pattern": r"wss?://", "scope": "strings"}]},
               {"id": "mlx", "label": "Apple MLX", "category": "inference", "description": "On-device models.", "signatures": [r"\bMLX\b"]},
           ],
           external_groups=[{"id": "on-device", "label": "On-device inference", "categories": ["inference"]}])
    write(root, "Sources/Store/ActivityStore.swift", SWIFT_STORE)
    write(root, "Sources/App/ChatView.swift", SWIFT_VIEW)
    return root


def test_swift_service_conforms_and_sees_what_portal_sees(tmp_path):
    model = compile_service(swift_service(tmp_path))
    assert validate_document(model) == []
    assert model["languages"] == ["swift"]
    # Census: every type and function counted, mapped or not.
    assert model["extraction"]["summary"]["by_kind"] == {
        "class": {"mapped": 1, "total": 1}, "enum": {"mapped": 1, "total": 1}, "function": {"mapped": 1, "total": 1}, "struct": {"mapped": 2, "total": 2},
    }
    # Portal's conventions: a *Store type is a store node, the RPC call an endpoint the caller invokes, the trigger a drives edge.
    store = next(n for n in model["interplay"]["nodes"] if n["id"] == "type:store:ActivityStore")
    assert store["kind"] == "store" and store["sub_kind"] == "class"
    assert ("type:store:ActivityStore", "endpoint:store:rpc_namespace:chat", "invokes", "interplay") in edges(model)
    assert ("type:app:ChatView", "type:store:ActivityStore", "drives", "interplay") in edges(model)
    assert ("type:store:ActivityStore", "type:store:Phase", "transitions", "lifecycle") in edges(model)
    assert ("entrypoint:app:Sources/App/ChatView.swift", "type:app:ChatView", "constructs", "structure") in edges(model)
    # Persistence and resources, cited by line.
    assert ("Sources/Store/ActivityStore.swift", 13, "swift.persistent_store", "store") in origins_of(model, "store:store:Sources/Store/ActivityStore.swift:13")
    assert ("Sources/Store/ActivityStore.swift", 15, "swift.http_client", "boundary") in origins_of(model, "resource:store:Sources/Store/ActivityStore.swift:15")
    assert ("Sources/Store/ActivityStore.swift", 12, "swift.state_machine", "behaviour") in origins_of(model, "type:store:Phase")
    fired = passes(model)
    for rule in ("swift.type", "swift.entrypoint", "swift.rpc_call", "swift.ui_trigger", "swift.store_declaration", "swift.concurrency",
                 "swift.persistent_store", "swift.http_client", "swift.state_machine", "swift.import_external", "module", "boundary.external_signature"):
        assert rule in fired and fired[rule]["class"] == "mechanical", rule
    assert fired["swift.ui_trigger"]["citations"] == 2
    assert fired["swift.type"]["citations"] == 4


def test_declared_external_systems_absorb_matching_packages_and_form_boundary_groups(tmp_path):
    model = compile_service(swift_service(tmp_path))
    externals = {s["id"]: s for s in model["externals"]["systems"]}
    assert set(externals) == {"gateway", "mlx"}, "MLX the package was absorbed by the declared system; no separate package node"
    assert externals["gateway"]["hit_count"] == 1 and externals["gateway"]["paths"] == ["Sources/Store/ActivityStore.swift"]
    assert externals["mlx"]["hit_count"] == 2 and externals["mlx"]["description_authority"] == "specified"
    assert externals["gateway"]["component"] == "store"
    assert nodes_by_kind(model, "external") == ["external:gateway", "external:mlx"]
    assert ("type:store:ActivityStore", "external:gateway", "uses", "boundary") in edges(model)
    assert ("type:store:ActivityStore", "external:mlx", "uses", "boundary") in edges(model)
    assert ("Sources/Store/ActivityStore.swift", 9, "boundary.external_signature", "boundary") in origins_of(model, "external:gateway")
    assert model["interplay"]["boundary_groups"] == [
        {"id": "on-device", "label": "On-device inference", "description": "", "categories": ["inference"], "members": ["external:mlx"]}
    ]
    invariants = {i["id"]: i for i in model["interplay"]["invariants"]}
    assert invariants["externals-declared-and-observed"]["status"] == "holds" and invariants["externals-declared-and-observed"]["checked"] == 2
    assert invariants["gates-declared"]["checked"] == 1


def test_silent_declared_system_and_empty_group_fail_closed(tmp_path):
    root = swift_service(tmp_path)
    cfg = json.loads((root / "architecture/config.json").read_text())
    cfg["external_systems"].append({"id": "ghost", "label": "Ghost", "category": "backend", "description": "Never used.", "signatures": [r"\bGhostSDK\b"]})
    write(root, "architecture/config.json", json.dumps(cfg))
    with pytest.raises(CompileError, match="never observed.*ghost"):
        compile_service(root)
    cfg["external_systems"].pop()
    cfg["external_groups"].append({"id": "empty", "label": "Empty", "categories": ["nothing"]})
    write(root, "architecture/config.json", json.dumps(cfg))
    with pytest.raises(CompileError, match="external group 'empty' has no members"):
        compile_service(root)
    cfg["external_groups"] = [{"id": "a", "label": "A", "categories": ["inference"]}, {"id": "b", "label": "B", "categories": ["inference"]}]
    write(root, "architecture/config.json", json.dumps(cfg))
    with pytest.raises(CompileError, match="belongs to both"):
        compile_service(root)


# ── TypeScript ───────────────────────────────────────────────────────────────

TS_SERVER = '''import express from "express";
import { writeFileSync } from "fs";
import { helper } from "./lib/helper";
const app = express();
enum Phase { Idle, Busy }
export class Digest {
  phase: Phase = Phase.Idle;
  async run(): Promise<void> {
    const res = await fetch("https://api.telegram.org/bot/sendMessage");
    writeFileSync("digest.json", "{}");
    this.phase = Phase.Busy;
    process.env.TOKEN;
  }
}
app.get("/health", (req, res) => res.send("ok"));
app.listen(3000);
'''


def test_typescript_service(tmp_path):
    root = tmp_path / "ts"
    config(root, external_systems=[{"id": "telegram", "label": "Telegram", "category": "messaging", "description": "Bot API.",
                                     "signatures": [{"pattern": r"api\.telegram\.org", "scope": "strings"}]}])
    write(root, "src/server.ts", TS_SERVER)
    write(root, "src/lib/helper.js", "module.exports = { helper: () => 1 };\n")
    model = compile_service(root)
    assert validate_document(model) == []
    assert nodes_by_kind(model, "external") == ["external:express", "external:telegram"], "relative and builtin imports are not externals"
    assert "endpoint:core:route:/health" in nodes_by_kind(model, "endpoint")
    assert ("type:core:Digest", "store:core:src/server.ts:10", "persists-to", "lifecycle") in edges(model)
    assert ("type:core:Digest", "resource:core:src/server.ts:9", "holds", "lifecycle") in edges(model)
    assert ("type:core:Digest", "type:core:Phase", "transitions", "lifecycle") in edges(model)
    assert ("type:core:Digest", "external:telegram", "uses", "boundary") in edges(model)
    fired = passes(model)
    assert fired["typescript.state_machine"]["citations"] == 2
    assert fired["typescript.route"]["citations"] == 1 and fired["typescript.socket_or_server"]["citations"] == 1
    assert fired["typescript.env_read"]["citations"] == 1 and fired["typescript.entrypoint"]["citations"] == 1
    census = {(d["kind"], d["name"]) for f in model["extraction"]["files"] for d in f["declarations"]}
    assert {("class", "Digest"), ("enum", "Phase"), ("function", "run")} <= census
    js = next(f for f in model["extraction"]["files"] if f["path"].endswith("helper.js"))
    assert js["language"] == "typescript" and js["touched"]


# ── Go ───────────────────────────────────────────────────────────────────────

GO_MAIN = '''package main

import (
\t"database/sql"
\t"net/http"
\t"os/exec"

\t"example.com/demo/internal/x"
\t"github.com/lib/pq"
)

type Store struct{ db *sql.DB }

func (s *Store) Save() error {
\t_, err := exec.Command("ls").Output()
\tgo s.work()
\treturn err
}

func main() {
\tdb, _ := sql.Open("postgres", "")
\ts := &Store{db: db}
\thttp.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {})
\thttp.ListenAndServe(":8080", nil)
\t_ = s
}
'''


def test_go_service(tmp_path):
    root = tmp_path / "go"
    config(root, local_modules=["example.com/demo"])
    write(root, "cmd/main.go", GO_MAIN)
    model = compile_service(root)
    assert validate_document(model) == []
    assert nodes_by_kind(model, "external") == ["external:github.com/lib/pq"], "stdlib and the module's own packages are not externals"
    assert "endpoint:core:route:/health" in nodes_by_kind(model, "endpoint")
    assert ("entrypoint:core:cmd/main.go", "type:core:Store", "constructs", "structure") in edges(model)
    fired = passes(model)
    for rule in ("go.route", "go.process_spawn", "go.concurrency", "go.persistent_store", "go.socket_or_server", "go.entrypoint", "go.type", "go.import_external"):
        assert rule in fired, rule
    census = {(d["kind"], d["name"]) for f in model["extraction"]["files"] for d in f["declarations"]}
    assert {("struct", "Store"), ("function", "Save"), ("function", "main")} <= census
    assert ("cmd/main.go", 21, "go.persistent_store", "store") in origins_of(model, "store:core:cmd/main.go:21")


# ── Rust ─────────────────────────────────────────────────────────────────────

RUST_MAIN = '''use std::fs;
use reqwest::Client;
use crate::store::Store;

pub struct Digest { client: Client, phase: Phase }
enum Phase { Idle, Busy }

impl Digest {
    pub async fn run(&mut self) {
        tokio::spawn(async {});
        fs::write("digest.json", "{}").unwrap();
        self.phase = Phase::Busy;
        let _ = reqwest::get("https://example.org").await;
    }
}

#[get("/health")]
fn health() {}

fn main() { }
'''


def test_rust_service(tmp_path):
    root = tmp_path / "rs"
    config(root)
    write(root, "src/main.rs", RUST_MAIN)
    model = compile_service(root)
    assert validate_document(model) == []
    assert nodes_by_kind(model, "external") == ["external:reqwest"], "std and crate:: are not externals"
    assert "endpoint:core:route:/health" in nodes_by_kind(model, "endpoint")
    assert ("type:core:Digest", "type:core:Phase", "transitions", "lifecycle") in edges(model)
    assert ("type:core:Digest", "store:core:src/main.rs:11", "persists-to", "lifecycle") in edges(model)
    assert ("type:core:Digest", "resource:core:src/main.rs:13", "holds", "lifecycle") in edges(model)
    fired = passes(model)
    for rule in ("rust.concurrency", "rust.file_write", "rust.http_client", "rust.route", "rust.state_machine", "rust.entrypoint", "rust.type"):
        assert rule in fired, rule
    census = {(d["kind"], d["name"]) for f in model["extraction"]["files"] for d in f["declarations"]}
    assert {("struct", "Digest"), ("enum", "Phase"), ("impl", "Digest"), ("function", "run"), ("function", "health"), ("function", "main")} <= census


# ── Python on the tree-sitter floor ──────────────────────────────────────────

PY_GLUE = '''import os
import requests
from enum import Enum

class Phase(Enum):
    IDLE = 1
    BUSY = 2

class Glue:
    def __init__(self):
        self.phase = Phase.IDLE
    def run(self):
        requests.get("https://api.telegram.org/bot")
        self.phase = Phase.BUSY
        os.getenv("TOKEN")

if __name__ == "__main__":
    Glue().run()
'''


def test_python_pack_matches_the_ast_compilers_findings(tmp_path):
    root = tmp_path / "py"
    config(root)
    write(root, "glue.py", PY_GLUE)
    model = compile_service(root)
    assert validate_document(model) == []
    assert nodes_by_kind(model, "external") == ["external:requests"]
    phase = next(n for n in model["interplay"]["nodes"] if n["id"] == "type:core:Phase")
    assert phase["sub_kind"] == "enum"
    assert ("type:core:Glue", "type:core:Phase", "transitions", "lifecycle") in edges(model)
    assert ("entrypoint:core:glue.py", "type:core:Glue", "constructs", "structure") in edges(model)
    fired = passes(model)
    assert fired["python.state_machine"]["citations"] == 2 and fired["python.http_client"]["citations"] == 1 and fired["python.env_read"]["citations"] == 1
    # The ast-based compiler agrees on the constructs it shares with this pack.
    from tools.architecture_compile_python import compile_service as compile_python
    reference = compile_python(root)
    assert {n["label"] for n in reference["interplay"]["nodes"] if n["kind"] == "class"} == {"Phase", "Glue"}
    assert {n["label"] for n in model["interplay"]["nodes"] if n["kind"] == "type"} == {"Phase", "Glue"}


# ── Mixed languages, declared additions, project rules ───────────────────────


def test_mixed_language_service_resolves_names_per_component(tmp_path):
    root = tmp_path / "mixed"
    config(root, components=[{"id": "web", "label": "Web", "description": "TS.", "patterns": ["web/**"]},
                             {"id": "rs", "label": "Rust", "description": "Rust.", "patterns": ["rs/**"]}])
    write(root, "web/server.ts", TS_SERVER.replace('fetch("https://api.telegram.org/bot/sendMessage")', 'fetch("https://example.org")'))
    write(root, "rs/src/main.rs", RUST_MAIN)
    model = compile_service(root)
    assert validate_document(model) == []
    assert model["languages"] == ["rust", "typescript"]
    assert model["inventory"]["by_language"] == {"rust": {"files": 1, "lines": 20}, "typescript": {"files": 1, "lines": 16}}
    # Two enums named Phase: each transition edge stays inside its own component.
    assert ("type:web:Digest", "type:web:Phase", "transitions", "lifecycle") in edges(model)
    assert ("type:rs:Digest", "type:rs:Phase", "transitions", "lifecycle") in edges(model)
    assert not any(s.startswith("type:web") and t.startswith("type:rs") for s, t, _, _ in edges(model, "transitions"))


def test_declared_additions_and_project_rules(tmp_path):
    root = tmp_path / "declared"
    config(root,
           declared={
               "nodes": [{"id": "decl:queue", "kind": "resource", "label": "Work queue", "path": "glue.py", "line": 9, "component": "core"}],
               "edges": [{"source": "type:core:Glue", "target": "decl:queue", "relation": "feeds"}],
               "flows": [{"id": "run", "title": "Run", "steps": [{"from": "type:core:Glue", "to": "decl:queue", "relation": "feeds"}]}],
               "invariants": [{"id": "queue-bounded", "kind": "queue_bounded", "why": "The queue never grows past 100."}],
           },
           rules=[{"id": "telegram_send", "description": "A Telegram send call", "family": "boundary", "language": "python",
                   "regex": r"\brequests\.get\(", "scope": "code", "produces": {"kind": "resource", "sub_kind": "telegram", "relation": "notifies", "edge_class": "interplay"}},
                  {"id": "class_query", "description": "Every class, by query", "family": "wiring", "language": "python",
                   "query": "(class_definition name: (identifier) @label) @site"}])
    write(root, "glue.py", PY_GLUE)
    model = compile_service(root)
    assert validate_document(model) == []
    fired = passes(model)
    assert fired["python.telegram_send"]["class"] == "mechanical" and fired["python.telegram_send"]["citations"] == 1
    assert fired["python.class_query"]["citations"] == 2
    assert fired["declared.node"]["class"] == "semantic"
    assert ("type:core:Glue", "resource:core:glue.py:13", "notifies", "interplay") in edges(model)
    assert ("type:core:Glue", "decl:queue", "feeds", "usage") in edges(model)
    assert model["interplay"]["flows"][0]["authority"] == "declared"
    assert {i["id"]: i["status"] for i in model["interplay"]["invariants"]}["queue-bounded"] == "unchecked"
    declared_file = next(f for f in model["extraction"]["files"] if f["path"] == "glue.py")
    assert declared_file["semantic_citations"] == 1 and declared_file["touched"]
    # A declared edge to nothing, and a rule for an unknown language, fail closed.
    cfg = json.loads((root / "architecture/config.json").read_text())
    cfg["declared"]["edges"].append({"source": "type:core:Glue", "target": "ghost", "relation": "feeds"})
    write(root, "architecture/config.json", json.dumps(cfg))
    with pytest.raises(CompileError, match="ghost"):
        compile_service(root)
    cfg["declared"]["edges"].pop()
    cfg["rules"].append({"id": "x", "description": "x", "family": "boundary", "language": "cobol", "regex": "x"})
    write(root, "architecture/config.json", json.dumps(cfg))
    with pytest.raises(CompileError, match="cobol"):
        load_extended_config(root)


def test_unassigned_file_fails_closed(tmp_path):
    root = tmp_path / "unassigned"
    config(root, components=[{"id": "core", "label": "Core", "description": "Only src.", "patterns": ["src/**"]}])
    write(root, "src/a.go", "package main\nfunc main() {}\n")
    write(root, "stray.rs", "fn main() {}\n")
    with pytest.raises(CompileError, match="unassigned: stray.rs"):
        compile_service(root)


# ── CLI: write, --check, staleness, determinism ──────────────────────────────


def test_cli_writes_checks_and_is_byte_deterministic(tmp_path):
    root = swift_service(tmp_path)
    command = [sys.executable, "-m", "tools.architecture_compiler", str(root)]
    first = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    model_path = root / "architecture/model/model.json"
    bytes_one = model_path.read_bytes()
    assert subprocess.run(command + ["--check"], cwd=REPO, capture_output=True, text=True).returncode == 0
    second = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert second.returncode == 0 and model_path.read_bytes() == bytes_one, "two compiles of the same tree are identical bytes"
    assert serialized(compile_service(root)).encode("utf-8") == bytes_one
    write(root, "Sources/Store/Extra.swift", "import Foundation\nstruct Extra {}\n")
    stale = subprocess.run(command + ["--check"], cwd=REPO, capture_output=True, text=True)
    assert stale.returncode == 1 and "stale" in stale.stderr
    assert model_path.read_bytes() == bytes_one, "--check never writes"
    # A failing compile leaves the committed model untouched.
    write(root, "Sources/Store/Stray.unknownlang", "")  # ignored: no pack claims it
    cfg = json.loads((root / "architecture/config.json").read_text())
    cfg["external_systems"].append({"id": "ghost", "label": "Ghost", "category": "backend", "description": "d", "signatures": ["GhostSDK"]})
    write(root, "architecture/config.json", json.dumps(cfg))
    failed = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    assert failed.returncode == 1 and "never observed" in failed.stderr and model_path.read_bytes() == bytes_one
    model = json.loads(bytes_one)
    assert model["compiler"] == "tools/architecture_compiler"
    assert model["ci"]["static_checks"][0]["command"] == CHECK_COMMAND and model["ci"]["merge"]["inputs"] == ["local/check"]


# ── The packs themselves ─────────────────────────────────────────────────────


def test_every_pack_rule_table_is_well_formed_and_queries_compile():
    import tree_sitter_language_pack as language_pack
    from tree_sitter import Query

    assert set(PACKS) == {"swift", "typescript", "go", "rust", "python"}
    for pack in PACKS.values():
        ids = [rule.id for rule in pack.rules]
        assert len(ids) == len(set(ids)), pack.name
        assert f"{pack.name}.import_external" in ids and f"{pack.name}.state_machine" in ids
        for rule in pack.rules:
            assert rule.id.startswith(pack.name + "."), rule.id
            assert rule.description.strip(), rule.id
            assert rule.family in FAMILIES, rule.id
            if rule.scope == "query":
                Query(language_pack.get_language(pack.grammar), rule.pattern)
            else:
                re.compile(rule.pattern)
        for extension in pack.extensions:
            assert pack_for_path(Path(f"x{extension}")) is pack
            language_pack.get_language(pack.grammar_for(f"x{extension}"))
        assert pack.limitations, f"{pack.name} must state what it cannot see"
    assert pack_for_path(Path("x.unknownlang")) is None
    with pytest.raises(ValueError):
        Rule("swift.bad", "d", "nonsense")
    with pytest.raises(ValueError):
        Rule("swift.bad", "d", "boundary", produces="galaxy")


def test_project_rule_normalises_ids_and_rejects_bad_edges():
    language, rule = project_rule({"id": "x", "description": "d", "family": "store", "language": "go", "regex": "x", "produces": {"kind": "store"}})
    assert language == "go" and rule.id == "go.x" and rule.wiring == ("persists-to", "lifecycle")
    with pytest.raises(CompileError, match="edge_class"):
        project_rule({"id": "x", "description": "d", "family": "store", "language": "go", "regex": "x", "produces": {"kind": "store", "edge_class": "magic"}})
    with pytest.raises(CompileError, match="regex"):
        project_rule({"id": "x", "description": "d", "family": "store", "language": "go"})


# ── Completeness: what the map is short is on the map ────────────────────────

PY_STORE = '''import json

class SessionStore:
    def __init__(self, path):
        self.path = path
    def save(self, sessions):
        with open(self.path, "w") as handle:
            json.dump(sessions, handle)
    def load(self):
        return {}
'''
PY_API = '''import requests
from flask import Flask
from app.store import SessionStore
from app import store as storemod

app = Flask(__name__)
PREFIX = "/api"
_store = SessionStore("x.json")

@app.get("/api/sessions")
def list_sessions():
    return _store.load()

@app.post(PREFIX + "/sessions")
def save_sessions():
    _store.save({})
    _store.frobnicate()
    storemod.nothing()
    return "ok"

class Client:
    def __init__(self, store: SessionStore):
        self.store = store
    def ping(self):
        self.store.load()
        self.store.missing()
        requests.get("http://localhost:8000/api/sessions")
        requests.get("http://localhost:8000/api/ghost")
        requests.get("https://api.telegram.org/bot")
'''


def invariants_of(model):
    return {i["id"]: i for i in model["interplay"]["invariants"]}


def unparsed_sites(model, invariant_id, key):
    return [(u["path"], u["line"], u[key]) for u in invariants_of(model)[invariant_id]["unparsed"]]


def test_completeness_invariants_name_every_unparsed_site(tmp_path):
    root = tmp_path / "complete"
    config(root, external_systems=[{"id": "telegram", "label": "Telegram", "category": "messaging", "description": "Bot API.",
                                     "signatures": [{"pattern": r"api\.telegram\.org", "scope": "strings"}]}])
    write(root, "app/store.py", PY_STORE)
    write(root, "app/api.py", PY_API)
    model = compile_service(root)
    assert validate_document(model) == []
    found = invariants_of(model)
    # A route whose path is computed is counted, not silently dropped.
    assert found["routes-declared-literally"]["status"] == "violated" and found["routes-declared-literally"]["checked"] == 2
    assert unparsed_sites(model, "routes-declared-literally", "rule") == [("app/api.py", 14, "python.route")]
    # Calls on a store-typed receiver — a module binding, a constructor-injected field, a module alias — resolve to members the store declares, or are listed.
    assert found["store-calls-resolve"]["checked"] == 6
    assert unparsed_sites(model, "store-calls-resolve", "method") == [("app/api.py", 17, "frobnicate"), ("app/api.py", 18, "nothing"), ("app/api.py", 26, "missing")]
    # A client path under the service's own route prefix must match a route; Telegram's URL is not under it and is not judged.
    assert found["client-paths-resolve"]["checked"] == 2
    assert unparsed_sites(model, "client-paths-resolve", "client_path") == [("app/api.py", 28, "/api/ghost")]
    # Packages no declared system claims are listed with the import that pulls them in.
    assert unparsed_sites(model, "vendors-declared", "package") == [("app/api.py", 1, "requests"), ("app/api.py", 2, "flask")]
    assert model["extraction"]["summary"]["completeness"]["store-calls-resolve"] == {"seen": 6, "parsed": 3, "unparsed": 3}
    # Declaring the packages as systems makes the invariant hold without touching the code.
    cfg = json.loads((root / "architecture/config.json").read_text())
    cfg["external_systems"] += [{"id": "http", "label": "requests", "category": "http", "description": "d", "signatures": [r"\brequests\b"]},
                                {"id": "flask", "label": "Flask", "category": "web", "description": "d", "signatures": [r"\bflask\b"]}]
    write(root, "architecture/config.json", json.dumps(cfg))
    assert invariants_of(compile_service(root))["vendors-declared"]["status"] == "holds"


TS_TAB_STORE = '''import { writeFileSync } from "fs";
export class ItemStore {
\tsave(items: string[]): void {
\t\twriteFileSync("items.json", JSON.stringify(items));
\t}
\tload(): string[] { return []; }
}
'''
TS_TAB_SERVER = '''import express from "express";
import { ItemStore } from "./store";
const app = express();
const store = new ItemStore();
const ROUTE = "/items";
app.get("/items", (req, res) => res.json(store.load()));
app.post(ROUTE, (req, res) => { store.save([]); store.purge(); res.send("ok"); });
app.listen(3000);
'''


def test_tab_indented_members_resolve_like_two_space_ones(tmp_path):
    root = tmp_path / "tabs"
    config(root)
    write(root, "src/store.ts", TS_TAB_STORE)
    write(root, "src/server.ts", TS_TAB_SERVER)
    model = compile_service(root)
    assert validate_document(model) == []
    assert unparsed_sites(model, "routes-declared-literally", "rule") == [("src/server.ts", 7, "typescript.route")]
    assert invariants_of(model)["store-calls-resolve"]["checked"] == 3
    assert unparsed_sites(model, "store-calls-resolve", "method") == [("src/server.ts", 7, "purge")]
    assert invariants_of(model)["client-paths-resolve"] == {"id": "client-paths-resolve", "kind": "client_paths_resolve", "status": "holds", "checked": 0,
                                                            "why": invariants_of(model)["client-paths-resolve"]["why"], "unparsed": []}


def test_a_probe_is_a_regex_in_the_rules_own_scope():
    assert Rule("go.x", "d", "trigger", "raw", r"a\(\"b\"", probe=r"a\(").probe == r"a\("
    with pytest.raises(ValueError, match="probe"):
        Rule("go.x", "d", "trigger", "query", "(call_expression) @site", probe=r"a\(")
    for pack in PACKS.values():
        for rule in pack.rules:
            if rule.probe:
                re.compile(rule.probe)
                assert rule.produces == "endpoint", f"{rule.id}: probes count the registrations the map may be short of"


# ── Usage rules on the call index ────────────────────────────────────────────

PY_REACH_STORE = '''import json

class SessionStore:
    def save(self, sessions):
        with open("sessions.json", "w") as handle:
            json.dump(sessions, handle)
    def load(self):
        return {}

def write_index(payload):
    with open("index.json", "w") as handle:
        json.dump(payload, handle)

def record(payload):
    write_index(payload)
'''
PY_REACH_API = '''import requests
from flask import Flask
from app.store import SessionStore
from app import store as storemod

app = Flask(__name__)
_store = SessionStore()

class Notifier:
    def __init__(self, store: SessionStore):
        self.store = store
    def notify(self, text):
        self.store.save({"text": text})
        requests.post("https://api.telegram.org/bot/send", json={"text": text})

_notifier = Notifier(_store)

@app.post("/api/sessions")
def save_sessions():
    _notifier.notify("saved")
    storemod.record({})
    return "ok"

@app.get("/api/sessions/<int:session_id>")
def one_session(session_id):
    return _store.load()

class ApiClient:
    def __init__(self, base):
        self.base = base
    def call(self, path, method="GET"):
        return requests.request(method, self.base + "/api" + path)
    def sessions(self):
        return self.call("/sessions")
    def ghosts(self):
        return self.call("/ghosts")
'''


def usage_edges(model, rule):
    return sorted(((e["source"], e["target"], e["relation"], e["class"], tuple(e.get("via") or []), e.get("hops"), e.get("composed"))
                   for e in model["interplay"]["edges"] if e.get("rule") == rule))


def test_injected_method_follows_a_handler_to_the_stores_and_systems_it_reaches(tmp_path):
    root = tmp_path / "reach"
    config(root, external_systems=[{"id": "telegram", "label": "Telegram", "category": "messaging", "description": "Bot API.",
                                     "signatures": [{"pattern": r"api\.telegram\.org", "scope": "strings"}]}])
    write(root, "app/store.py", PY_REACH_STORE)
    write(root, "app/api.py", PY_REACH_API)
    model = compile_service(root)
    assert validate_document(model) == []
    endpoint = "endpoint:core:route:/api/sessions"
    # Through a module-level object whose class took the store in its constructor (two hops, chain on the edge) …
    assert (endpoint, "store:core:app/store.py:5", "uses", "usage", ("Notifier.notify", "SessionStore.save"), 2, None) in usage_edges(model, "usage.injected_method")
    # … through a module imported under a name, to a function it calls in turn …
    assert (endpoint, "store:core:app/store.py:11", "uses", "usage", ("record", "write_index"), 2, None) in usage_edges(model, "usage.injected_method")
    # … and to the external system the first hop's body names.
    assert (endpoint, "external:telegram", "touches", "boundary", ("Notifier.notify",), 1, None) in usage_edges(model, "usage.injected_method")
    # A handler that reaches no mechanism adds nothing; the direct citations stay with their owners.
    assert not any(s == "endpoint:core:route:/api/sessions/-int-session_id" for s, *_ in usage_edges(model, "usage.injected_method"))
    assert ("type:core:SessionStore", "store:core:app/store.py:5", "persists-to", "lifecycle") in edges(model)
    fired = passes(model)
    assert fired["usage.injected_method"]["class"] == "mechanical" and fired["usage.injected_method"]["citations"] == 2, "one citation per first-hop call line in the handler"
    assert {("app/api.py", 20, "usage.injected_method", "wiring"), ("app/api.py", 21, "usage.injected_method", "wiring")} <= origins_of(model, endpoint)
    # A client wrapper: the member spelling "/api" + path lends the prefix; the sibling that passes "/sessions" invokes the route.
    assert usage_edges(model, "usage.client_wrapper") == [("type:core:ApiClient", endpoint, "invokes", "interplay", ("ApiClient.call",), None, "/api/sessions")]
    assert unparsed_sites(model, "client-paths-resolve", "client_path") == [("app/api.py", 36, "/api/ghosts")]
    assert invariants_of(model)["client-paths-resolve"]["checked"] == 2, "the wrapper's own prefix literal is not a client path"
    assert invariants_of(model)["store-calls-resolve"]["status"] == "holds"


TS_REACH_STORE = '''import { writeFileSync } from "fs";
export class ItemStore {
  save(items: string[]): void {
    writeFileSync("items.json", JSON.stringify(items));
  }
}
export class ItemService {
  constructor(private readonly store: ItemStore) {}
  add(item: string): void {
    this.store.save([item]);
  }
}
'''
TS_REACH_SERVER = '''import express from "express";
import { ItemStore, ItemService } from "./store";
const app = express();
const service = new ItemService(new ItemStore());
app.post("/api/items", (req, res) => { service.add(req.body); res.send("ok"); });
app.get("/api/spaces/:id/threads", (req, res) => res.json([]));
app.listen(3000);
'''
TS_REACH_CLIENT = '''const space = (id: string) => `/spaces/${id}`;
export class Client {
  constructor(private base: string) {}
  request(path: string, init?: RequestInit) {
    return fetch(this.base + `/api${path}`, init);
  }
  threads(id: string) {
    return this.request(`${space(id)}/threads`);
  }
  missing() {
    return this.request("/nowhere");
  }
}
'''


def test_typescript_inline_handlers_constructor_properties_and_template_helpers(tmp_path):
    root = tmp_path / "ts-reach"
    config(root)
    write(root, "src/store.ts", TS_REACH_STORE)
    write(root, "src/server.ts", TS_REACH_SERVER)
    write(root, "src/client.ts", TS_REACH_CLIENT)
    model = compile_service(root)
    assert validate_document(model) == []
    # The inline arrow handler's span is the start; `service` is a module binding; `store` is a constructor parameter property.
    assert usage_edges(model, "usage.injected_method") == [
        ("endpoint:core:route:/api/items", "store:core:src/store.ts:4", "uses", "usage", ("ItemService.add", "ItemStore.save"), 2, None)]
    # `${space(id)}/threads` through the wrapper `/api${path}` with the module's template helper inlined is /api/spaces/${id}/threads.
    assert usage_edges(model, "usage.client_wrapper") == [
        ("type:core:Client", "endpoint:core:route:/api/spaces/-id/threads", "invokes", "interplay", ("Client.request",), None, "/api/spaces/${id}/threads")]
    assert unparsed_sites(model, "client-paths-resolve", "client_path") == [("src/client.ts", 11, "/api/nowhere")]


def test_the_harness_gateway_handlers_reach_their_stores_through_module_imports(tmp_path):
    """The proof on harness itself: an RPC handler that imports a store module inside its body
    reaches the module's JSON write through two further calls, chain recorded."""
    import shutil

    root = tmp_path / "gw"
    shutil.copytree(REPO / "tui_gateway", root / "tui_gateway", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config(root, components=[{"id": "gateway", "label": "Gateway", "description": "The JSON-RPC gateway.", "patterns": ["tui_gateway/**"]}],
           languages=["python"],
           rules=[{"id": "rpc_method", "description": "A JSON-RPC method registration: @method(\"namespace.name\")", "family": "trigger", "language": "python",
                   "scope": "raw", "regex": r"@(?:\w+\.)?method\(\s*\"([^\"]+)\"", "produces": {"kind": "endpoint", "sub_kind": "rpc", "label_group": 1}}])
    model = compile_service(root)
    assert validate_document(model) == []
    reached = {(s, t): via for s, t, _, _, via, _, _ in usage_edges(model, "usage.injected_method")}
    describe = [(t, via) for (s, t), via in reached.items() if s == "endpoint:gateway:rpc:architecture.describe"]
    assert describe, "architecture.describe reaches a store site"
    target, via = describe[0]
    assert target.startswith("store:gateway:tui_gateway/architecture_store.py:") and via[0] == "describe" and via[-1] == "_write_json"
    assert len(reached) >= 40, f"many handlers reach a mechanism through the modules they import: {len(reached)}"
    found = invariants_of(model)
    assert found["store-calls-resolve"]["status"] == "holds", found["store-calls-resolve"]["unparsed"]
    assert found["routes-declared-literally"]["status"] == "holds"


# ── What a process does for its life: ticks, state, tables ───────────────────

PY_PROC_MAIN = '''import sys
from svc import jobs
from svc.store import Registry

_registry = Registry()

def main():
    jobs.start_reaper()
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        _registry.put(raw)
        jobs.schedule(raw)

if __name__ == "__main__":
    main()
'''
PY_PROC_JOBS = '''import threading
import time

_QUEUE: list = []
_SEEN = {}

def schedule(item):
    _QUEUE.append(item)

def _reap():
    while True:
        time.sleep(5)
        drain()

def drain():
    while _QUEUE:
        _QUEUE.pop()

def start_reaper():
    threading.Thread(target=_reap, daemon=True).start()

def retry(fn):
    def attempt():
        while True:
            time.sleep(1)
            fn()
    return attempt

def wait_for(ready):
    attempts = 0
    while attempts < 3:
        time.sleep(1)
        attempts += 1
'''
PY_PROC_STORE = '''class Registry:
    def __init__(self):
        self.items = {}
    def put(self, key):
        self.items[key] = 1

class Unused:
    def __init__(self):
        self.cache = {}
    def add(self, key):
        self.cache[key] = 1
'''
PY_PROC_SCHEMA = '''SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL
);
CREATE TABLE tags (
    session_id TEXT,
    name TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""
'''


def relation_edges(model, relation):
    return sorted((e["source"], e["target"], tuple(e.get("via") or []), {k: v for k, v in e.items() if k in ("interval", "column", "target_column", "on_delete")})
                  for e in model["interplay"]["edges"] if e["relation"] == relation)


def test_python_process_ticks_state_and_foreign_keys(tmp_path):
    from tui_gateway.architecture_contract import KNOWN_NODE_KINDS, KNOWN_RELATIONS

    root = tmp_path / "proc"
    config(root)
    write(root, "svc/main.py", PY_PROC_MAIN)
    write(root, "svc/jobs.py", PY_PROC_JOBS)
    write(root, "svc/store.py", PY_PROC_STORE)
    write(root, "svc/schema.py", PY_PROC_SCHEMA)
    model = compile_service(root)
    assert validate_document(model) == []
    nodes = {n["id"]: n for n in model["interplay"]["nodes"]}
    entry = "entrypoint:core:svc/main.py"
    # Ticks: the entry's own read-paced loop, and the timer loop handed to a thread one import away. Not the
    # unpaced drain loop, not the closure handed out by retry, not the bounded wait_for retry.
    assert nodes_by_kind(model, "tick") == ["tick:core:svc/jobs.py:11", "tick:core:svc/main.py:9"]
    main_loop, reaper = nodes["tick:core:svc/main.py:9"], nodes["tick:core:svc/jobs.py:11"]
    assert main_loop["sub_kind"] == "loop" and main_loop["pacing"] == "read" and main_loop["label"] == "main loop"
    assert [s["call"] for s in main_loop["steps"]] == ["sys.stdin.readline", "_registry.put", "jobs.schedule"], "the body's calls in source order — the only order the compiler claims"
    assert reaper["sub_kind"] == "starter" and reaper["interval"] == "5" and reaper["holder"] == "_reap"
    assert relation_edges(model, "runs") == [(entry, "tick:core:svc/jobs.py:11", ("svc/jobs.py",), {"interval": "5"}), (entry, "tick:core:svc/main.py:9", (), {})]
    # State: the written module list and the field of the class main.py constructs; not the never-written _SEEN, not the unconstructed Unused.cache.
    assert nodes_by_kind(model, "state") == ["state:core:svc/jobs.py:4", "state:core:svc/store.py:3"]
    assert nodes["state:core:svc/store.py:3"]["label"] == "Registry.items" and nodes["state:core:svc/jobs.py:4"]["sub_kind"] == "list"
    keeps = relation_edges(model, "keeps")
    assert (entry, "state:core:svc/jobs.py:4", ("svc/jobs.py",), {}) in keeps and (entry, "state:core:svc/store.py:3", ("svc/store.py",), {}) in keeps
    # Which loop writes which state, through what: the chain on the edge.
    assert ("tick:core:svc/main.py:9", "state:core:svc/store.py:3", ("main", "Registry.put"), {}) in keeps
    assert ("tick:core:svc/main.py:9", "state:core:svc/jobs.py:4", ("main", "schedule"), {}) in keeps
    assert ("tick:core:svc/jobs.py:11", "state:core:svc/jobs.py:4", ("_reap", "drain"), {}) in keeps
    # Tables and foreign keys, column-level and table-level, with the delete rule.
    assert nodes_by_kind(model, "table") == ["table:core:session_model_usage", "table:core:sessions", "table:core:tags"]
    assert nodes["table:core:session_model_usage"]["columns"] == ["session_id", "model"]
    assert relation_edges(model, "references") == [
        ("table:core:session_model_usage", "table:core:sessions", (), {"column": "session_id", "target_column": "id", "on_delete": "CASCADE"}),
        ("table:core:tags", "table:core:sessions", (), {"column": "session_id", "target_column": "id"}),
    ]
    fired = passes(model)
    for rule in ("python.launch.loop", "python.launch.starter", "python.state.resident", "python.schema.table", "python.schema.references"):
        assert rule in fired and fired[rule]["class"] == "mechanical", rule
    assert ("svc/jobs.py", 11, "python.launch.starter", "behaviour") in origins_of(model, "tick:core:svc/jobs.py:11")
    # The vocabulary stays the contract's.
    assert {n["kind"] for n in model["interplay"]["nodes"]} <= set(KNOWN_NODE_KINDS)
    assert {e["relation"] for e in model["interplay"]["edges"]} <= set(KNOWN_RELATIONS)


def test_the_harness_gateway_runs_its_loops_and_keeps_its_registries(tmp_path):
    """The proof on harness: the gateway's stdin loop and its background workers appear as ticks with
    their intervals, and the subscription registry is state the query poller writes."""
    import shutil

    root = tmp_path / "gw"
    shutil.copytree(REPO / "tui_gateway", root / "tui_gateway", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config(root, components=[{"id": "gateway", "label": "Gateway", "description": "The JSON-RPC gateway.", "patterns": ["tui_gateway/**"]}], languages=["python"])
    model = compile_service(root)
    nodes = {n["id"]: n for n in model["interplay"]["nodes"]}
    ticks = {n["label"]: n for n in nodes.values() if n["kind"] == "tick" and n["path"] != "tui_gateway/slash_worker.py"}  # the slash worker is a process of its own
    assert ticks["main loop"]["path"] == "tui_gateway/entry.py" and ticks["main loop"]["sub_kind"] == "loop" and ticks["main loop"]["pacing"] == "read"
    assert ticks["_start_idle_reaper._loop loop"]["interval"] == "_REAPER_SCAN_S" and ticks["_start_idle_reaper._loop loop"]["sub_kind"] == "starter"
    assert ticks["_poll_loop loop"]["interval"] == "1.0"
    assert "_wake_resume_if_owner._retry loop" not in ticks, "a deadline-bounded retry is not a schedule"
    entry = "entrypoint:gateway:tui_gateway/entry.py"
    runs = {e["target"] for e in model["interplay"]["edges"] if e["source"] == entry and e["relation"] == "runs"}
    assert ticks["main loop"]["id"] in runs and ticks["_start_idle_reaper._loop loop"]["id"] in runs
    assert ticks["main loop"]["id"] not in {e["target"] for e in model["interplay"]["edges"] if e["source"] != entry and e["relation"] == "runs"}, "another entry's top level does not run this one's loop"
    subscriptions = next(n for n in nodes.values() if n["kind"] == "state" and n["label"] == "_SUBSCRIPTIONS")
    poller_keeps = [e for e in model["interplay"]["edges"] if e["source"] == ticks["_poll_loop loop"]["id"] and e["target"] == subscriptions["id"]]
    assert poller_keeps and poller_keeps[0]["via"] == ["_poll_loop", "run_due"]
    sessions = next(n for n in nodes.values() if n["kind"] == "state" and n["label"] == "_sessions" and n["path"] == "tui_gateway/server.py")
    assert (entry, sessions["id"]) in {(e["source"], e["target"]) for e in model["interplay"]["edges"] if e["relation"] == "keeps"}
    assert nodes_by_kind(model, "table") == [], "the gateway declares no SQL tables of its own"


def test_the_harness_state_schema_foreign_keys_match_the_statements(tmp_path):
    import shutil

    root = tmp_path / "hs"
    shutil.copy(REPO / "hermes_state_schema.py", root / "hermes_state_schema.py") if root.mkdir() is None else None
    config(root)
    model = compile_service(root)
    tables = {n["label"]: n for n in model["interplay"]["nodes"] if n["kind"] == "table"}
    assert set(tables) == {"gateway_routing", "session_model_usage", "sessions"}, "two CREATE TABLE statements (one issued twice) and the table they reference"
    assert tables["sessions"]["sub_kind"] == "referenced", "sessions is created in another module; it is on the map as the referenced table"
    assert relation_edges(model, "references") == [
        ("table:core:session_model_usage", "table:core:sessions", (), {"column": "session_id", "target_column": "id", "on_delete": "CASCADE"})]
