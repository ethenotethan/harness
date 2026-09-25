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
